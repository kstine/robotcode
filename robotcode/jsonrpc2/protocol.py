from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
    runtime_checkable,
)

from ..utils.async_tools import (
    HasThreaded,
    async_event,
    create_sub_future,
    create_sub_task,
    run_coroutine_in_thread,
)
from ..utils.dataclasses import as_json, from_dict
from ..utils.inspect import ensure_coroutine, iter_methods
from ..utils.logging import LoggingDescriptor

__all__ = [
    "JsonRPCErrors",
    "JsonRPCMessage",
    "JsonRPCNotification",
    "JsonRPCRequest",
    "JsonRPCResponse",
    "JsonRPCError",
    "JsonRPCErrorObject",
    "JsonRPCProtocol",
    "JsonRPCException",
    "JsonRPCParseError",
    "InvalidProtocolVersionError",
    "rpc_method",
    "RpcRegistry",
    "JsonRPCProtocolPart",
    "ProtocolPartDescriptor",
    "GenericJsonRPCProtocolPart",
    "TProtocol",
    "JsonRPCErrorException",
]

_T = TypeVar("_T")
_TResult = TypeVar("_TResult", bound=Any)


class JsonRPCErrors:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_ERROR_START = -32000
    SERVER_ERROR_END = -32099

    REQUEST_CANCELLED = -32800


PROTOCOL_VERSION = "2.0"


@dataclass
class JsonRPCMessage:
    jsonrpc: str = field(default=PROTOCOL_VERSION, init=False)


@dataclass
class JsonRPCNotification(JsonRPCMessage):
    method: str
    params: Optional[Any] = None


@dataclass
class JsonRPCRequest(JsonRPCMessage):
    id: Union[int, str, None]
    method: str
    params: Optional[Any] = None


@dataclass
class _JsonRPCResponseBase(JsonRPCMessage):
    id: Union[int, str, None]


@dataclass
class JsonRPCResponse(_JsonRPCResponseBase):
    result: Any


@dataclass
class JsonRPCErrorObject:
    code: int
    message: Optional[str]
    data: Optional[Any] = None


@dataclass
class JsonRPCError(_JsonRPCResponseBase):
    error: JsonRPCErrorObject
    result: Optional[Any] = None


class JsonRPCException(Exception):
    pass


class JsonRPCErrorException(JsonRPCException):
    def __init__(self, code: int, message: Optional[str], data: Optional[Any] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class JsonRPCParseError(JsonRPCException):
    pass


class InvalidProtocolVersionError(JsonRPCParseError):
    pass


class RpcMethodEntry(NamedTuple):
    name: str
    method: Callable[..., Any]
    param_type: Optional[Type[Any]]
    cancelable: bool


@runtime_checkable
class RpcMethod(Protocol):
    __rpc_method__: RpcMethodEntry


_F = TypeVar("_F", bound=Callable[..., Any])


@overload
def rpc_method(_func: _F) -> _F:
    ...


@overload
def rpc_method(
    *, name: Optional[str] = None, param_type: Optional[Type[Any]] = None, cancelable: bool = True
) -> Callable[[_F], _F]:
    ...


def rpc_method(
    _func: Optional[_F] = None,
    *,
    name: Optional[str] = None,
    param_type: Optional[Type[Any]] = None,
    cancelable: bool = True,
) -> Callable[[_F], _F]:
    def _decorator(func: _F) -> Callable[[_F], _F]:

        if inspect.isclass(_func):
            raise TypeError(f"Not supported type {type(func)}.")

        if isinstance(func, classmethod):
            f = func.__func__
        elif isinstance(func, staticmethod):
            f = func.__func__
        else:
            f = func

        real_name = name if name is not None else f.__name__ if f is not None else None
        if real_name is None or not real_name:
            raise ValueError("name is empty.")

        cast(RpcMethod, f).__rpc_method__ = RpcMethodEntry(real_name, f, param_type, cancelable)
        return func

    if _func is None:
        return cast(Callable[[_F], _F], _decorator)
    return _decorator(_func)


@runtime_checkable
class HasRpcRegistry(Protocol):
    __rpc_registry__: RpcRegistry


class RpcRegistry:
    _class_registries: Dict[Type[Any], RpcRegistry] = {}

    def __init__(self, owner: Any = None) -> None:
        self.__owner = owner
        self.__methods: Dict[str, RpcMethodEntry] = {}
        self.__initialized = False
        self.__class_parts: Dict[str, Type[Any]] = {}
        self.__class_part_instances: weakref.WeakSet[GenericJsonRPCProtocolPart[Any]] = weakref.WeakSet()

    def __set_name__(self, owner: Any, name: str) -> None:
        self.__owner = owner

    def __get__(self, obj: Any, obj_type: Type[Any]) -> "RpcRegistry":
        if obj is None and obj_type == self.__owner:
            return self

        if obj is not None:
            if not isinstance(obj, HasRpcRegistry):
                cast(HasRpcRegistry, obj).__rpc_registry__ = RpcRegistry(obj)

            return cast(HasRpcRegistry, obj).__rpc_registry__

        if obj_type not in RpcRegistry._class_registries:
            RpcRegistry._class_registries[obj_type] = RpcRegistry(obj_type)

        return RpcRegistry._class_registries[obj_type]

    def _reset(self) -> None:
        self.__methods.clear()
        self.__initialized = False

    def add_class_part(self, name: str, class_type: Type[Any]) -> None:
        self._reset()
        self.__class_parts[name] = class_type

    def add_class_part_instance(self, instance: GenericJsonRPCProtocolPart[Any]) -> None:
        self._reset()
        self.__class_part_instances.add(instance)

    @property
    def parts(self) -> Set[GenericJsonRPCProtocolPart[Any]]:
        self.__ensure_initialized()

        return set(self.__class_part_instances)

    def __ensure_initialized(self) -> None:
        def get_methods(obj: Any) -> Dict[str, RpcMethodEntry]:
            return {
                rpc_method.__rpc_method__.name: RpcMethodEntry(
                    rpc_method.__rpc_method__.name,
                    method,
                    rpc_method.__rpc_method__.param_type,
                    rpc_method.__rpc_method__.cancelable,
                )
                for method, rpc_method in map(
                    lambda m1: (m1, cast(RpcMethod, m1)),
                    filter(lambda m2: isinstance(m2, RpcMethod), iter_methods(obj)),
                )
            }

        if not self.__initialized:

            if inspect.isclass(self.__owner):
                self.__methods = get_methods(self.__owner)

                for cp in self.__class_parts.items():
                    self.__methods.update(get_methods(cp))
            else:
                registries: List[RpcRegistry] = []
                for m in inspect.getmro(type(self.__owner)):
                    r = self._class_registries.get(m, None)
                    if r is not None:
                        registries.insert(0, r)
                for r in registries:
                    for k in r.__class_parts.keys():
                        getattr(self.__owner, k)

                self.__methods = get_methods(self.__owner)

                for cpi in self.__class_part_instances:
                    self.__methods.update(get_methods(cpi))

        self.__initialized = True

    def initialize_parts(self) -> None:
        self.__ensure_initialized()

    @property
    def methods(self) -> Dict[str, RpcMethodEntry]:
        self.__ensure_initialized()

        return self.__methods

    def add_method(
        self, name: str, func: Callable[..., Any], param_type: Optional[Type[Any]] = None, cancelable: bool = True
    ) -> None:
        self.__ensure_initialized()

        self.__methods[name] = RpcMethodEntry(name, func, param_type, cancelable)

    def remove_method(self, name: str) -> Optional[RpcMethodEntry]:
        self.__ensure_initialized()
        return self.__methods.pop(name, None)

    def get_entry(self, name: str) -> Optional[RpcMethodEntry]:
        self.__ensure_initialized()
        return self.__methods.get(name, None)

    def get_method(self, name: str) -> Optional[Callable[..., Any]]:
        result = self.get_entry(name)
        if result is None:
            return None
        return result.method

    def get_param_type(self, name: str) -> Optional[Type[Any]]:
        result = self.get_entry(name)
        if result is None:
            return None
        return result.param_type


class SendedRequestEntry(NamedTuple):
    future: asyncio.Future[Any]
    result_type: Optional[Type[Any]]


class ReceivedRequestEntry(NamedTuple):
    future: asyncio.Future[Any]
    request: Optional[Any]
    cancelable: bool


class JsonRPCProtocolBase(asyncio.Protocol, ABC):

    _logger = LoggingDescriptor()
    registry = RpcRegistry()

    def __init__(self) -> None:
        self.read_transport: Optional[asyncio.ReadTransport] = None
        self.write_transport: Optional[asyncio.WriteTransport] = None
        self._message_buf = bytes()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @async_event
    async def on_connection_made(sender, transport: asyncio.BaseTransport) -> None:
        ...

    @async_event
    async def on_connection_lost(sender, exc: Optional[BaseException]) -> None:
        ...

    @_logger.call
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._loop = asyncio.get_running_loop()
        if isinstance(transport, asyncio.ReadTransport):
            self.read_transport = transport
        if isinstance(transport, asyncio.WriteTransport):
            self.write_transport = transport

        create_sub_task(self.on_connection_made(self, transport))

    @_logger.call
    def connection_lost(self, exc: Optional[BaseException]) -> None:
        create_sub_task(self.on_connection_lost(self, exc))
        self._loop = None

    @_logger.call
    def eof_received(self) -> Optional[bool]:
        return False

    CHARSET = "utf-8"
    CONTENT_TYPE = "application/vscode-jsonrpc"

    MESSAGE_PATTERN = re.compile(
        rb"(?:[^\r\n]*\r\n)*"
        + rb"(Content-Length: ?(?P<length>\d+)\r\n)"
        + rb"((Content-Type: ?(?P<content_type>[^\r\n;]+)"
        + rb"(; *(charset=(?P<charset>[^\r\n]+))?)?\r\n)|(?:[^\r\n]+\r\n))*"
        + rb"\r\n(?P<body>.*)",
        re.DOTALL,
    )

    def data_received(self, data: bytes) -> None:
        while len(data):
            # Append the incoming chunk to the message buffer
            self._message_buf += data

            # Look for the body of the message
            found = self.MESSAGE_PATTERN.match(self._message_buf)

            body = found.group("body") if found else b""
            length = int(found.group("length")) if found else 1

            charset = (
                found.group("charset").decode("ascii") if found and found.group("charset") is not None else self.CHARSET
            )

            if len(body) < length:
                return

            body, data = body[:length], body[length:]
            self._message_buf = bytes()

            self._handle_body(body, charset)

    @abstractmethod
    def _handle_body(self, body: bytes, charset: str) -> None:
        ...


class JsonRPCProtocol(JsonRPCProtocolBase):
    _logger = LoggingDescriptor()
    _data_logger = LoggingDescriptor(postfix="_data")

    def __init__(self) -> None:
        super().__init__()
        self._sended_request_lock = threading.RLock()
        self._sended_request: OrderedDict[Union[str, int], SendedRequestEntry] = OrderedDict()
        self._sended_request_count = 0
        self._received_request: OrderedDict[Union[str, int, None], ReceivedRequestEntry] = OrderedDict()
        self._received_request_lock = threading.RLock()

    @staticmethod
    def _generate_json_rpc_messages_from_dict(
        data: Union[Dict[Any, Any], List[Dict[Any, Any]]]
    ) -> Iterator[JsonRPCMessage]:
        def inner(d: Dict[Any, Any]) -> JsonRPCMessage:
            if "jsonrpc" in d:
                if d["jsonrpc"] != PROTOCOL_VERSION:
                    raise InvalidProtocolVersionError("Invalid JSON-RPC2 protocol version.")
                d.pop("jsonrpc")

                return from_dict(
                    d,
                    (
                        JsonRPCRequest,
                        JsonRPCResponse,
                        JsonRPCNotification,
                        JsonRPCError,
                    ),
                )

            raise JsonRPCException("Invalid JSON-RPC2 Message")

        if isinstance(data, list):
            for e in data:
                yield inner(e)
        else:
            yield inner(data)

    def _handle_body(self, body: bytes, charset: str) -> None:
        try:
            b = body.decode(charset)

            self._data_logger.trace(lambda: f"JSON Received: {b}")

            self._handle_messages(self._generate_json_rpc_messages_from_dict(json.loads(b)))
        except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            self._logger.exception(e)
            self.send_error(JsonRPCErrors.PARSE_ERROR, f"{type(e).__name__}: {e}")

    def _handle_messages(self, iterator: Iterator[JsonRPCMessage]) -> None:
        def done(f: asyncio.Future[Any]) -> None:
            if f.done() and not f.cancelled():
                ex = f.exception()

                if ex is None or isinstance(ex, asyncio.CancelledError):
                    return

        for m in iterator:
            create_sub_task(self.handle_message(m)).add_done_callback(done)

    @_logger.call
    async def handle_message(self, message: JsonRPCMessage) -> None:
        if isinstance(message, JsonRPCRequest):
            await self.handle_request(message)
        elif isinstance(message, JsonRPCNotification):
            await self.handle_notification(message)
        elif isinstance(message, JsonRPCError):
            await self.handle_error(message)
        elif isinstance(message, JsonRPCResponse):
            await self.handle_response(message)

    @_logger.call
    def send_response(self, id: Optional[Union[str, int, None]], result: Optional[Any] = None) -> None:
        self.send_message(JsonRPCResponse(id=id, result=result))

    @_logger.call
    def send_error(
        self,
        code: int,
        message: Optional[str],
        id: Optional[Union[str, int, None]] = None,
        data: Optional[Any] = None,
    ) -> None:
        error_obj = JsonRPCErrorObject(code=code, message=message)
        if data is not None:
            error_obj.data = data

        self.send_message(
            JsonRPCError(
                id=id,
                error=error_obj,
            )
        )

    @_logger.call
    def send_message(self, message: JsonRPCMessage) -> None:
        message.jsonrpc = PROTOCOL_VERSION

        body = as_json(message, compact=True).encode(self.CHARSET)

        header = (
            f"Content-Length: {len(body)}\r\nContent-Type: {self.CONTENT_TYPE}; charset={self.CHARSET}\r\n\r\n"
        ).encode("ascii")

        if self.write_transport is not None:
            msg = header + body

            self._data_logger.trace(lambda: f"JSON send: {msg.decode()}")

            if self._loop:
                self._loop.call_soon_threadsafe(self.write_transport.write, msg)

    @_logger.call
    def send_request(
        self,
        method: str,
        params: Optional[Any] = None,
        return_type_or_converter: Optional[Type[_TResult]] = None,
    ) -> asyncio.Future[Optional[_TResult]]:

        with self._sended_request_lock:
            result: asyncio.Future[Optional[_TResult]] = create_sub_future()
            self._sended_request_count += 1
            id = self._sended_request_count

            self._sended_request[id] = SendedRequestEntry(result, return_type_or_converter)

        request = JsonRPCRequest(id=id, method=method, params=params)
        self.send_message(request)

        return result

    async def send_request_async(
        self,
        method: str,
        params: Optional[Any] = None,
        return_type: Optional[Type[_TResult]] = None,
    ) -> Optional[_TResult]:

        return await self.send_request(method, params, return_type)

    @_logger.call
    def send_notification(self, method: str, params: Any) -> None:
        self.send_message(JsonRPCNotification(method=method, params=params))

    @_logger.call(exception=True)
    async def handle_response(self, message: JsonRPCResponse) -> None:
        if message.id is None:
            error = "Invalid response. Response id is null."
            self._logger.warning(error)
            self.send_error(JsonRPCErrors.INTERNAL_ERROR, error)
            return

        with self._sended_request_lock:
            entry = self._sended_request.pop(message.id, None)

        if entry is None:
            error = f"Invalid response. Could not find id '{message.id}' in request list."
            self._logger.warning(error)
            self.send_error(JsonRPCErrors.INTERNAL_ERROR, error)
            return

        try:
            if not entry.future.done():
                res = None
                if message.result is not None:
                    res = from_dict(message.result, entry.result_type)
                if entry.future._loop == asyncio.get_running_loop():
                    entry.future.set_result(res)
                else:
                    if entry.future._loop.is_running():

                        def s(f: asyncio.Future[Any], r: Any) -> None:
                            if not f.done():
                                f.set_result(r)

                        entry.future._loop.call_soon_threadsafe(s, entry.future, res)
                    else:
                        self._logger.warning("Response loop is not running.")

        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            if not entry.future.done():
                if entry.future._loop == asyncio.get_running_loop():
                    entry.future.set_exception(e)
                else:
                    entry.future._loop.call_soon_threadsafe(entry.future.set_exception, e)

    @_logger.call
    async def handle_error(self, message: JsonRPCError) -> None:
        raise JsonRPCErrorException(message.error.code, message.error.message, message.error.data)

    @staticmethod
    def _convert_params(
        callable: Callable[..., Any], params_type: Optional[Type[Any]], params: Any
    ) -> Tuple[List[Any], Dict[str, Any]]:
        if params is None:
            return [], {}
        if params_type is None:
            if isinstance(params, Mapping):
                return [], dict(**params)
            else:
                return [params], {}

        # try to convert the dict to correct type
        converted_params = from_dict(params, params_type)

        signature = inspect.signature(callable)

        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())

        kw_args = {}
        args = []
        params_added = False
        rest = set(converted_params.__dict__.keys())
        if isinstance(params, dict):
            rest = set.union(rest, params.keys())

        for v in signature.parameters.values():
            if v.name in converted_params.__dict__:
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(getattr(converted_params, v.name))
                else:
                    kw_args[v.name] = getattr(converted_params, v.name)
                rest.remove(v.name)
            elif v.name == "params":
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(converted_params)
                    params_added = True
                else:
                    kw_args[v.name] = converted_params
                    params_added = True
            elif isinstance(params, dict) and v.name in params:
                if v.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(params[v.name])
                else:
                    kw_args[v.name] = params[v.name]
        if has_var_kw:
            for r in rest:
                if hasattr(converted_params, r):
                    kw_args[r] = getattr(converted_params, r)
                elif isinstance(params, dict) and r in params:
                    kw_args[r] = params[r]

            if not params_added:
                kw_args["params"] = converted_params
        return args, kw_args

    @_logger.call
    async def handle_request(self, message: JsonRPCRequest) -> None:
        e = self.registry.get_entry(message.method)

        if e is None or not callable(e.method):
            self.send_error(
                JsonRPCErrors.METHOD_NOT_FOUND,
                f"Unknown method: {message.method}",
                id=message.id,
            )
            return None

        params = self._convert_params(e.method, e.param_type, message.params)

        if isinstance(e.method, HasThreaded) and cast(HasThreaded, e.method).__threaded__:
            task = run_coroutine_in_thread(ensure_coroutine(e.method), *params[0], **params[1])
        else:
            task = create_sub_task(
                ensure_coroutine(e.method)(*params[0], **params[1]),
                name=message.method,
            )

        with self._received_request_lock:
            self._received_request[message.id] = ReceivedRequestEntry(task, message, e.cancelable)

        def done(t: asyncio.Task[Any]) -> None:
            try:
                self.send_response(message.id, t.result())
            except asyncio.CancelledError:
                self._logger.debug(f"request message {repr(message)} canceled")
                self.send_error(JsonRPCErrors.REQUEST_CANCELLED, "Request canceled.", id=message.id)
            except (SystemExit, KeyboardInterrupt):
                raise
            except JsonRPCErrorException as e:
                self.send_error(e.code, e.message or f"{type(e).__name__}: {e}", id=message.id, data=e.data)
            except BaseException as e:
                self._logger.exception(e)
                self.send_error(JsonRPCErrors.INTERNAL_ERROR, f"{type(e).__name__}: {e}", id=message.id)
            finally:
                with self._received_request_lock:
                    self._received_request.pop(message.id, None)

        task.add_done_callback(done)

        await task

    @_logger.call
    def cancel_request(self, id: Union[int, str, None]) -> None:
        with self._received_request_lock:
            entry = self._received_request.get(id, None)

        if entry is not None and entry.future is not None and not entry.future.cancelled():
            self._logger.debug(f"try to cancel request {entry.request}")
            entry.future.cancel()

    @_logger.call
    async def cancel_all_received_request(self) -> None:
        for entry in self._received_request.values():
            if entry is not None and entry.cancelable and entry.future is not None and not entry.future.cancelled():
                entry.future.cancel()

    @_logger.call
    async def handle_notification(self, message: JsonRPCNotification) -> None:
        e = self.registry.get_entry(message.method)

        if e is None or not callable(e.method):
            self._logger.warning(f"Unknown method: {message.method}")
            return
        try:
            params = self._convert_params(e.method, e.param_type, message.params)

            if isinstance(e.method, HasThreaded) and cast(HasThreaded, e.method).__threaded__:
                task = run_coroutine_in_thread(ensure_coroutine(e.method), *params[0], **params[1])
            else:
                task = create_sub_task(
                    ensure_coroutine(e.method)(*params[0], **params[1]),
                    name=message.method,
                )

            await task
        except asyncio.CancelledError:
            pass
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as e:
            self._logger.exception(e)


TProtocol = TypeVar("TProtocol", bound=JsonRPCProtocol)


class GenericJsonRPCProtocolPart(Generic[TProtocol]):
    def __init__(self, parent: TProtocol) -> None:
        self._parent = weakref.ref(parent)
        parent.registry.add_class_part_instance(self)

    @property
    def parent(self) -> TProtocol:
        result = self._parent()
        if result is None:
            raise JsonRPCException("WeakRef is dead.")
        return result


class JsonRPCProtocolPart(GenericJsonRPCProtocolPart[JsonRPCProtocol]):
    pass


TProtocolPart = TypeVar("TProtocolPart", bound=GenericJsonRPCProtocolPart[Any])


@runtime_checkable
class HasPartInstance(Protocol[TProtocolPart]):
    __rpc_part_instances__: Dict[Type[TProtocolPart], TProtocolPart]


class ProtocolPartDescriptor(Generic[TProtocolPart]):
    def __init__(self, instance_type: Type[TProtocolPart], *args: Any, **kwargs: Any):
        self._instance_type = instance_type
        self._instance_args = args
        self._instance_kwargs = kwargs

    def __set_name__(self, owner: Type[Any], name: str) -> None:
        if not issubclass(owner, JsonRPCProtocol):
            raise AttributeError()
        owner.registry.add_class_part(name, self._instance_type)

    def __get__(self, obj: Optional[Any], objtype: Type[Any]) -> TProtocolPart:
        if obj is not None:
            if not isinstance(obj, HasPartInstance):
                cast(HasPartInstance[TProtocolPart], obj).__rpc_part_instances__ = {}

            if self._instance_type not in cast(HasPartInstance[TProtocolPart], obj).__rpc_part_instances__:
                instance = self._instance_type(*(obj, *self._instance_args), **self._instance_kwargs)
                cast(HasPartInstance[TProtocolPart], obj).__rpc_part_instances__[self._instance_type] = instance

                cast(JsonRPCProtocol, obj).registry.add_class_part_instance(instance)

            return cast(HasPartInstance[TProtocolPart], obj).__rpc_part_instances__[self._instance_type]

        return self._instance_type  # type: ignore
