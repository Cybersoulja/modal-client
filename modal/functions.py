import asyncio
import platform
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Optional, Union

from aiostream import stream
from grpc import StatusCode
from grpc.aio import AioRpcError

from modal_proto import api_pb2
from modal_utils.async_utils import (
    queue_batch_iterator,
    synchronize_apis,
    warn_if_generator_is_not_consumed,
)
from modal_utils.grpc_utils import retry_transient_errors

from ._blob_utils import MAX_OBJECT_SIZE_BYTES, blob_download, blob_upload
from ._function_utils import FunctionInfo
from ._serialization import deserialize, serialize
from .exception import ExecutionError, InvalidError, NotFoundError, RemoteError
from .mount import _Mount
from .object import Object, Ref
from .rate_limit import RateLimit
from .schedule import Schedule
from .secret import _Secret
from .shared_volume import _SharedVolume


async def _process_result(result, stub, client=None):
    if result.WhichOneof("data_oneof") == "data_blob_id":
        data = await blob_download(result.data_blob_id, stub)
    else:
        data = result.data

    if result.status != api_pb2.GenericResult.GENERIC_STATUS_SUCCESS:
        if data:
            try:
                exc = deserialize(data, client)
            except Exception as deser_exc:
                raise ExecutionError(
                    "Could not deserialize remote exception due to local error:\n"
                    + f"{deser_exc}\n"
                    + "This can happen if your local environment does not have the remote exception definitions.\n"
                    + "Here is the remote traceback:\n"
                    + f"{result.traceback}"
                )
            if not isinstance(exc, BaseException):
                raise ExecutionError(f"Got remote exception of incorrect type {type(exc)}")

            raise exc
        raise RemoteError(result.exception)

    return deserialize(data, client)


async def _create_input(args, kwargs, client, idx=None) -> api_pb2.FunctionPutInputsItem:
    """Serialize function arguments and create a FunctionInput protobuf,
    uploading to blob storage if needed.
    """

    args_serialized = serialize((args, kwargs))

    if len(args_serialized) > MAX_OBJECT_SIZE_BYTES:
        args_blob_id = await blob_upload(args_serialized, client.stub)

        return api_pb2.FunctionPutInputsItem(
            input=api_pb2.FunctionInput(args_blob_id=args_blob_id),
            idx=idx,
        )
    else:
        return api_pb2.FunctionPutInputsItem(
            input=api_pb2.FunctionInput(args=args_serialized),
            idx=idx,
        )


@dataclass
class OutputValue:
    # box class for distinguishing None results from non-existing/None markers
    value: Any


class Invocation:
    def __init__(self, stub, function_call_id, client=None):
        self.stub = stub
        self.client = client  # Used by the deserializer.
        self.function_call_id = function_call_id

    @staticmethod
    async def create(function_id, args, kwargs, client):
        if not function_id:
            raise InvalidError(
                "The function has not been initialized.\n"
                "\n"
                "Modal functions can only be called within an app. "
                "Try calling it from another running modal function or from an app run context:\n\n"
                "with app.run():\n"
                "    my_modal_function()\n"
            )
        request = api_pb2.FunctionMapRequest(function_id=function_id)
        response = await retry_transient_errors(client.stub.FunctionMap, request)

        function_call_id = response.function_call_id

        item = await _create_input(args, kwargs, client)
        request_put = api_pb2.FunctionPutInputsRequest(
            function_id=function_id, inputs=[item], function_call_id=function_call_id
        )
        await retry_transient_errors(
            client.stub.FunctionPutInputs,
            request_put,
            max_retries=None,
            additional_status_codes=[StatusCode.RESOURCE_EXHAUSTED],
        )

        return Invocation(client.stub, function_call_id, client)

    async def get_items(self, timeout: float = None):
        t0 = time.time()
        if timeout is None:
            backend_timeout = 60.0
        else:
            backend_timeout = min(60.0, timeout)  # refresh backend call every 60s

        while True:
            # always execute at least one poll for results, regardless if timeout is 0
            request = api_pb2.FunctionGetOutputsRequest(
                function_call_id=self.function_call_id, timeout=backend_timeout, return_empty_on_timeout=True
            )
            response = await retry_transient_errors(
                self.stub.FunctionGetOutputs,
                request,
            )
            if len(response.outputs) > 0:
                for item in response.outputs:
                    yield item.result
                return

            if timeout is not None:
                # update timeout in retry loop
                backend_timeout = min(60.0, t0 + timeout - time.time())
                if backend_timeout < 0:
                    break

    async def run_function(self):
        result = (await stream.list(self.get_items()))[0]
        assert not result.gen_status
        return await _process_result(result, self.stub, self.client)

    async def poll_function(self, timeout: float = 0):
        results = await stream.list(self.get_items(timeout=timeout))

        if len(results) == 0:
            raise TimeoutError()

        return await _process_result(results[0], self.stub, self.client)

    async def run_generator(self):
        completed = False
        while not completed:
            async for result in self.get_items():
                if result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_COMPLETE:
                    completed = True
                    break
                yield await _process_result(result, self.stub, self.client)


MAP_INVOCATION_CHUNK_SIZE = 100


class _FunctionCall(Object, type_prefix="fc"):
    """A reference to an executed function call

    Constructed using `.submit(...)` on a Modal function with the same
    arguments that a function normally takes. Acts as a reference to
    an ongoing function call that can be passed around and used to
    poll or fetch function results at some later time.

    Conceptually similar to a Future/Promise/AsyncResult in other contexts and languages.
    """

    def _invocation(self):
        return Invocation(self._client.stub, self.object_id, self._client)

    async def get(self, timeout: Optional[float] = None):
        """Gets the result of the future

        Raises `TimeoutError` if no results are returned within `timeout` seconds.
        Setting `timeout` to None (the default) waits indefinitely until there is a result
        """
        return await self._invocation().poll_function(timeout=timeout)


FunctionCall, AioFunctionCall = synchronize_apis(_FunctionCall)


async def map_invocation(function_id, input_stream, kwargs, client, is_generator):
    request = api_pb2.FunctionMapRequest(function_id=function_id)
    response = await retry_transient_errors(client.stub.FunctionMap, request)

    function_call_id = response.function_call_id

    have_all_inputs = False
    num_outputs = 0
    num_inputs = 0

    input_queue: asyncio.Queue = asyncio.Queue()

    async def drain_input_generator():
        nonlocal num_inputs, input_queue
        async with input_stream.stream() as streamer:
            async for arg in streamer:
                item = await _create_input(arg, kwargs, client, idx=num_inputs)
                num_inputs += 1
                await input_queue.put(item)
        # close queue iterator
        await input_queue.put(None)
        yield

    async def pump_inputs():
        nonlocal num_inputs, have_all_inputs, input_queue

        async for items in queue_batch_iterator(input_queue, MAP_INVOCATION_CHUNK_SIZE):
            request = api_pb2.FunctionPutInputsRequest(
                function_id=function_id, inputs=items, function_call_id=function_call_id
            )
            await retry_transient_errors(
                client.stub.FunctionPutInputs,
                request,
                max_retries=None,
                additional_status_codes=[StatusCode.RESOURCE_EXHAUSTED],
            )

        have_all_inputs = True
        yield

    async def poll_outputs():
        nonlocal num_inputs, num_outputs, have_all_inputs

        # map to store out-of-order outputs received
        pending_outputs = {}

        while True:
            request = api_pb2.FunctionGetOutputsRequest(
                function_call_id=function_call_id, timeout=60, return_empty_on_timeout=True
            )
            response = await retry_transient_errors(
                client.stub.FunctionGetOutputs,
                request,
                max_retries=None,
                base_delay=0,
            )

            for item in response.outputs:
                if is_generator:
                    if item.result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_COMPLETE:
                        num_outputs += 1
                    else:
                        output = await _process_result(item.result, client.stub, client)
                        # yield output directly for generators.
                        yield OutputValue(output)
                else:
                    # hold on to outputs for function maps, so we can reorder them correctly.
                    pending_outputs[item.idx] = await _process_result(item.result, client.stub, client)

            # send outputs sequentially while we can
            while num_outputs in pending_outputs:
                output = pending_outputs.pop(num_outputs)
                yield OutputValue(output)
                num_outputs += 1

            if have_all_inputs:
                assert num_outputs <= num_inputs
                if num_outputs == num_inputs:
                    break

        assert len(pending_outputs) == 0

    response_gen = stream.merge(drain_input_generator(), pump_inputs(), poll_outputs())

    async with response_gen.stream() as streamer:
        async for response in streamer:
            # Handle yield at the end of pump_inputs, in case
            # that finishes after all outputs have been polled.
            if response is None:
                if have_all_inputs and num_outputs == num_inputs:
                    break
                continue
            yield response.value


class _Function(Object, type_prefix="fu"):
    """Functions are the basic units of serverless execution on Modal.

    Generally, you will not construct a `Function` directly. Instead, use the
    `@stub.function` decorator on the `Stub` object for your application.
    """

    # TODO: more type annotations
    _secrets: Collection[Union[Ref, _Secret]]

    def __init__(
        self,
        raw_f,
        image=None,
        secret: Optional[Union[Ref, _Secret]] = None,
        secrets: Collection[Union[Ref, _Secret]] = (),
        schedule: Optional[Schedule] = None,
        is_generator=False,
        gpu: bool = False,
        rate_limit: Optional[RateLimit] = None,
        # TODO: maybe break this out into a separate decorator for notebooks.
        serialized: bool = False,
        mounts: Collection[Union[Ref, _Mount]] = (),
        shared_volumes: Dict[str, Union[_SharedVolume, Ref]] = {},
        webhook_config: Optional[api_pb2.WebhookConfig] = None,
        memory: Optional[int] = None,
        proxy: Optional[Ref] = None,
        retries: Optional[int] = None,
        concurrency_limit: Optional[int] = None,
    ) -> None:
        """mdmd:hidden"""
        assert callable(raw_f)
        self._info = FunctionInfo(raw_f, serialized)
        if schedule is not None:
            if not self._info.is_nullary():
                raise InvalidError(
                    f"Function {raw_f} has a schedule, so it needs to support calling it with no arguments"
                )
        # assert not synchronizer.is_synchronized(image)

        self._raw_f = raw_f
        self._image = image
        if secret and secrets:
            raise InvalidError(f"Function {raw_f} has both singular `secret` and plural `secrets` attached")
        if secret:
            self._secrets = [secret]
        else:
            self._secrets = secrets

        if retries is not None and (not isinstance(retries, int) or retries < 0 or retries > 10):
            raise InvalidError(f"Function {raw_f} retries must be an integer between 0 and 10.")

        self._schedule = schedule
        self._is_generator = is_generator
        self._gpu = gpu
        self._rate_limit = rate_limit
        self._mounts = mounts
        self._shared_volumes = shared_volumes
        self._webhook_config = webhook_config
        self._web_url = None
        self._memory = memory
        self._proxy = proxy
        self._retries = retries
        self._concurrency_limit = concurrency_limit
        self._local_app = None
        self._local_object_id = None
        self._tag = self._info.get_tag()
        super().__init__()

    def initialize_from_proto(self, function: api_pb2.Function):
        self._is_generator = function.function_type == api_pb2.Function.FUNCTION_TYPE_GENERATOR

    def _get_creating_message(self) -> str:
        return f"Creating {self._tag}..."

    def _get_created_message(self) -> str:
        if self._web_url is not None:
            # TODO: this is only printed when we're showing progress. Maybe move this somewhere else.
            return f"Created {self._tag} => [magenta underline]{self._web_url}[/magenta underline]"
        return f"Created {self._tag}."

    async def _load(self, client, app_id, loader, existing_function_id):
        if self._proxy:
            proxy_id = await loader(self._proxy)
            # HACK: remove this once we stop using ssh tunnels for this.
            if self._image:
                self._image = self._image.run_commands(["apt-get install -yq ssh"])
        else:
            proxy_id = None

        # TODO: should we really join recursively here? Maybe it's better to move this logic to the app class?
        if self._image is not None:
            image_id = await loader(self._image)
        else:
            image_id = None  # Happens if it's a notebook function
        secret_ids = []
        for secret in self._secrets:
            try:
                secret_id = await loader(secret)
            except NotFoundError as ex:
                if isinstance(secret, Ref) and secret.tag is None:
                    msg = "Secret {!r} was not found".format(secret.app_name)
                else:
                    msg = str(ex)
                msg += ". You can add secrets to your account at https://modal.com/secrets"
                raise NotFoundError(msg)
            secret_ids.append(secret_id)

        mount_ids = []
        for mount in self._mounts:
            mount_ids.append(await loader(mount))

        if not isinstance(self._shared_volumes, dict):
            raise InvalidError("shared_volumes must be a dict[str, SharedVolume] where the keys are paths")
        shared_volume_mounts = []
        # Relies on dicts being ordered (true as of Python 3.6).
        for path, shared_volume in self._shared_volumes.items():
            # TODO: check paths client-side on Windows as well.
            if platform.system() != "Windows" and Path(path).resolve() != Path(path):
                raise InvalidError("Shared volume remote directory must be an absolute path.")

            shared_volume_mounts.append(
                api_pb2.SharedVolumeMount(mount_path=path, shared_volume_id=await loader(shared_volume))
            )

        if self._is_generator:
            function_type = api_pb2.Function.FUNCTION_TYPE_GENERATOR
        else:
            function_type = api_pb2.Function.FUNCTION_TYPE_FUNCTION

        rate_limit = self._rate_limit._to_proto() if self._rate_limit else None

        # Create function remotely
        function_definition = api_pb2.Function(
            module_name=self._info.module_name,
            function_name=self._info.function_name,
            mount_ids=mount_ids,
            secret_ids=secret_ids,
            image_id=image_id,
            definition_type=self._info.definition_type,
            function_serialized=self._info.function_serialized,
            function_type=function_type,
            resources=api_pb2.Resources(gpu=self._gpu, memory=self._memory),
            rate_limit=rate_limit,
            webhook_config=self._webhook_config,
            shared_volume_mounts=shared_volume_mounts,
            proxy_id=proxy_id,
            retry_policy=api_pb2.FunctionRetryPolicy(retries=self._retries),
            concurrency_limit=self._concurrency_limit,
        )
        request = api_pb2.FunctionCreateRequest(
            app_id=app_id,
            function=function_definition,
            schedule=self._schedule.proto_message if self._schedule is not None else None,
            existing_function_id=existing_function_id,
        )
        try:
            response = await client.stub.FunctionCreate(request)
        except AioRpcError as exc:
            if exc.code() == StatusCode.INVALID_ARGUMENT:
                raise InvalidError(exc.details())
            raise

        if response.web_url:
            # TODO(erikbern): we really shouldn't mutate the object here
            self._web_url = response.web_url

        return response.function_id

    @property
    def tag(self):
        return self._tag

    @property
    def web_url(self):
        # TODO(erikbern): it would be much better if this gets written to the "live" object,
        # and then we look it up from the app.
        return self._web_url

    def set_local_app(self, app):
        """mdmd:hidden"""
        self._local_app = app

    def _get_context(self):
        # Functions are sort of "special" in the sense that they are just global objects not attached to an app
        # the way other objects are. So in order to work with functions, we need to look up the running app
        # in runtime. Either we're inside a container, in which case it's a singleton, or we're in the client,
        # in which case we can set the running app on all functions when we run the app.
        if self._client and self._object_id:
            # Can happen if this is a function loaded from a different app or something
            return (self._client, self._object_id)

        # avoid circular import
        from .app import _container_app, is_local

        if is_local():
            if self._local_app is None:
                raise InvalidError(
                    "App is not running. You might need to put the function call inside a `with stub.run():` block."
                )
            app = self._local_app
        else:
            app = _container_app
        client = app.client
        object_id = app[self._tag].object_id
        return (client, object_id)

    async def _map(self, input_stream, kwargs={}):
        client, object_id = self._get_context()
        async for item in map_invocation(object_id, input_stream, kwargs, client, self._is_generator):
            yield item

    @warn_if_generator_is_not_consumed
    async def map(
        self,
        *input_iterators,  # one input iterator per argument in the mapped-over function/generator
        kwargs={},  # any extra keyword arguments for the function
    ):
        """Parallel map over a set of inputs.

        Takes one iterator argument per argument in the function being mapped over.

        Example:
        ```python notest
        @stub.function
        def my_func(a):
            return a ** 2

        assert list(my_func.starmap([1, 2, 3, 4])) == [1, 4, 9, 16]
        ```

        If applied to a `stub.function`, `map()` returns one result per input and the output order
        is guaranteed to be the same as the input order.

        If applied to a `stub.generator`, the results are returned as they are finished and can be
        out of order. By yielding zero or more than once, mapping over generators can also be used
        as a "flat map".
        """
        input_stream = stream.zip(*(stream.iterate(it) for it in input_iterators))
        async for item in self._map(input_stream, kwargs):
            yield item

    @warn_if_generator_is_not_consumed
    async def starmap(self, input_iterator, kwargs={}):
        """Like `map` but spreads arguments over multiple function arguments

        Assumes every input is a sequence (e.g. a tuple)

        Example:
        ```python notest
        @stub.function
        def my_func(a, b):
            return a + b

        assert list(my_func.starmap([(1, 2), (3, 4)])) == [3, 7]
        ```
        """
        input_stream = stream.iterate(input_iterator)
        async for item in self._map(input_stream, kwargs):
            yield item

    async def call_function(self, args, kwargs):
        """mdmd:hidden"""
        client, object_id = self._get_context()
        invocation = await Invocation.create(object_id, args, kwargs, client)
        return await invocation.run_function()

    async def call_function_nowait(self, args, kwargs):
        """mdmd:hidden"""
        client, object_id = self._get_context()
        return await Invocation.create(object_id, args, kwargs, client)

    @warn_if_generator_is_not_consumed
    async def call_generator(self, args, kwargs):
        """mdmd:hidden"""
        client, object_id = self._get_context()
        invocation = await Invocation.create(object_id, args, kwargs, client)
        async for res in invocation.run_generator():
            yield res

    async def call_generator_nowait(self, args, kwargs):
        """mdmd:hidden"""
        client, object_id = self._get_context()
        return await Invocation.create(object_id, args, kwargs, client)

    def __call__(self, *args, **kwargs):
        if self._is_generator:
            return self.call_generator(args, kwargs)
        else:
            return self.call_function(args, kwargs)

    async def enqueue(self, *args, **kwargs):
        """Calls the function with the given arguments, without waiting for the results.

        **Deprecated.** Use `.submit()` instead when possible.
        """
        warnings.warn("Function.enqueue is deprecated, use .submit() instead", DeprecationWarning)
        if self._is_generator:
            await self.call_generator_nowait(args, kwargs)
        else:
            await self.call_function_nowait(args, kwargs)

    async def submit(self, *args, **kwargs) -> Optional[_FunctionCall]:
        """Calls the function with the given arguments, without waiting for the results.

        Returns a `modal.functions.FunctionCall` object, that can later be polled or waited for using `.get(timeout=...)`.
        Conceptually similar to `multiprocessing.pool.apply_async`, or a Future/Promise in other contexts.

        *Note:* `.submit()` on a modal generator function does call and execute the generator, but does not currently
        return a function handle for polling the result.
        """
        if self._is_generator:
            await self.call_generator_nowait(args, kwargs)
            return None

        invocation = await self.call_function_nowait(args, kwargs)
        return _FunctionCall(invocation.client, invocation.function_call_id)

    def get_raw_f(self) -> Callable:
        """Return the inner Python object wrapped by this function."""
        return self._raw_f


Function, AioFunction = synchronize_apis(_Function)
