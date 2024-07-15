from __future__ import annotations

from multiprocessing.synchronize import Event as EventType
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Literal,
    NewType,
    Optional,
    Protocol,
    TypedDict,
    Union,
)

import h2.events
import h11

from .config import Config, Sockets

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired

H11SendableEvent = Union[h11.Data, h11.EndOfMessage, h11.InformationalResponse, h11.Response]

WorkerFunc = Callable[[Config, Optional[Sockets], Optional[EventType]], None]

LifespanState = Dict[str, Any]

ConnectionState = NewType("ConnectionState", Dict[str, Any])


class ASGIVersions(TypedDict, total=False):
    spec_version: str
    version: Literal["2.0"] | Literal["3.0"]


class HTTPScope(TypedDict):
    type: Literal["http"]
    asgi: ASGIVersions
    http_version: str
    method: str
    scheme: str
    path: str
    raw_path: bytes
    query_string: bytes
    root_path: str
    headers: Iterable[tuple[bytes, bytes]]
    client: tuple[str, int] | None
    server: tuple[str, int | None] | None
    state: ConnectionState
    extensions: dict[str, dict]


class WebsocketScope(TypedDict):
    type: Literal["websocket"]
    asgi: ASGIVersions
    http_version: str
    scheme: str
    path: str
    raw_path: bytes
    query_string: bytes
    root_path: str
    headers: Iterable[tuple[bytes, bytes]]
    client: tuple[str, int] | None
    server: tuple[str, int | None] | None
    subprotocols: Iterable[str]
    state: ConnectionState
    extensions: dict[str, dict]


class LifespanScope(TypedDict):
    type: Literal["lifespan"]
    asgi: ASGIVersions
    state: LifespanState


WWWScope = Union[HTTPScope, WebsocketScope]
Scope = Union[HTTPScope, WebsocketScope, LifespanScope]


class HTTPRequestEvent(TypedDict):
    type: Literal["http.request"]
    body: bytes
    more_body: bool


class HTTPResponseStartEvent(TypedDict):
    type: Literal["http.response.start"]
    status: int
    headers: Iterable[tuple[bytes, bytes]]
    trailers: NotRequired[bool]


class HTTPResponseBodyEvent(TypedDict):
    type: Literal["http.response.body"]
    body: bytes
    more_body: bool


class HTTPResponseTrailersEvent(TypedDict):
    type: Literal["http.response.trailers"]
    headers: Iterable[tuple[bytes, bytes]]
    more_trailers: NotRequired[bool]


class HTTPServerPushEvent(TypedDict):
    type: Literal["http.response.push"]
    path: str
    headers: Iterable[tuple[bytes, bytes]]


class HTTPEarlyHintEvent(TypedDict):
    type: Literal["http.response.early_hint"]
    links: Iterable[bytes]


class HTTPDisconnectEvent(TypedDict):
    type: Literal["http.disconnect"]


class WebsocketConnectEvent(TypedDict):
    type: Literal["websocket.connect"]


class WebsocketAcceptEvent(TypedDict):
    type: Literal["websocket.accept"]
    subprotocol: str | None
    headers: Iterable[tuple[bytes, bytes]]


class WebsocketReceiveEvent(TypedDict):
    type: Literal["websocket.receive"]
    bytes: bytes | None
    text: str | None


class WebsocketSendEvent(TypedDict):
    type: Literal["websocket.send"]
    bytes: bytes | None
    text: str | None


class WebsocketResponseStartEvent(TypedDict):
    type: Literal["websocket.http.response.start"]
    status: int
    headers: Iterable[tuple[bytes, bytes]]


class WebsocketResponseBodyEvent(TypedDict):
    type: Literal["websocket.http.response.body"]
    body: bytes
    more_body: bool


class WebsocketDisconnectEvent(TypedDict):
    type: Literal["websocket.disconnect"]
    code: int


class WebsocketCloseEvent(TypedDict):
    type: Literal["websocket.close"]
    code: int
    reason: str | None


class LifespanStartupEvent(TypedDict):
    type: Literal["lifespan.startup"]


class LifespanShutdownEvent(TypedDict):
    type: Literal["lifespan.shutdown"]


class LifespanStartupCompleteEvent(TypedDict):
    type: Literal["lifespan.startup.complete"]


class LifespanStartupFailedEvent(TypedDict):
    type: Literal["lifespan.startup.failed"]
    message: str


class LifespanShutdownCompleteEvent(TypedDict):
    type: Literal["lifespan.shutdown.complete"]


class LifespanShutdownFailedEvent(TypedDict):
    type: Literal["lifespan.shutdown.failed"]
    message: str


ASGIReceiveEvent = Union[
    HTTPRequestEvent,
    HTTPDisconnectEvent,
    WebsocketConnectEvent,
    WebsocketReceiveEvent,
    WebsocketDisconnectEvent,
    LifespanStartupEvent,
    LifespanShutdownEvent,
]


ASGISendEvent = Union[
    HTTPResponseStartEvent,
    HTTPResponseBodyEvent,
    HTTPResponseTrailersEvent,
    HTTPServerPushEvent,
    HTTPEarlyHintEvent,
    HTTPDisconnectEvent,
    WebsocketAcceptEvent,
    WebsocketSendEvent,
    WebsocketResponseStartEvent,
    WebsocketResponseBodyEvent,
    WebsocketCloseEvent,
    LifespanStartupCompleteEvent,
    LifespanStartupFailedEvent,
    LifespanShutdownCompleteEvent,
    LifespanShutdownFailedEvent,
]


ASGIReceiveCallable = Callable[[], Awaitable[ASGIReceiveEvent]]
ASGISendCallable = Callable[[ASGISendEvent], Awaitable[None]]

ASGIFramework = Callable[
    [
        Scope,
        ASGIReceiveCallable,
        ASGISendCallable,
    ],
    Awaitable[None],
]
WSGIFramework = Callable[[dict, Callable], Iterable[bytes]]
Framework = Union[ASGIFramework, WSGIFramework]


class H2SyncStream(Protocol):
    scope: dict

    def data_received(self, data: bytes) -> None:
        ...

    def ended(self) -> None:
        ...

    def reset(self) -> None:
        ...

    def close(self) -> None:
        ...

    async def handle_request(
        self,
        event: h2.events.RequestReceived,
        scheme: str,
        client: tuple[str, int],
        server: tuple[str, int],
    ) -> None:
        ...


class H2AsyncStream(Protocol):
    scope: dict

    async def data_received(self, data: bytes) -> None:
        ...

    async def ended(self) -> None:
        ...

    async def reset(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def handle_request(
        self,
        event: h2.events.RequestReceived,
        scheme: str,
        client: tuple[str, int],
        server: tuple[str, int],
    ) -> None:
        ...


class Event(Protocol):
    def __init__(self) -> None:
        ...

    async def clear(self) -> None:
        ...

    async def set(self) -> None:
        ...

    async def wait(self) -> None:
        ...

    def is_set(self) -> bool:
        ...


class WorkerContext(Protocol):
    event_class: type[Event]
    single_task_class: type[SingleTask]
    terminate: Event
    terminated: Event

    async def mark_request(self) -> None:
        ...

    @staticmethod
    async def sleep(wait: float | int) -> None:
        ...

    @staticmethod
    def time() -> float:
        ...


class TaskGroup(Protocol):
    async def spawn_app(
        self,
        app: AppWrapper,
        config: Config,
        scope: Scope,
        send: Callable[[ASGISendEvent | None], Awaitable[None]],
    ) -> Callable[[ASGIReceiveEvent], Awaitable[None]]:
        ...

    def spawn(self, func: Callable, *args: Any) -> None:
        ...

    async def __aenter__(self) -> TaskGroup:
        ...

    async def __aexit__(self, exc_type: type, exc_value: BaseException, tb: TracebackType) -> None:
        ...


class ResponseSummary(TypedDict):
    status: int
    headers: Iterable[tuple[bytes, bytes]]


class AppWrapper(Protocol):
    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
        call_soon: Callable,
    ) -> None:
        ...


class SingleTask(Protocol):
    def __init__(self) -> None:
        ...

    async def restart(self, task_group: TaskGroup, action: Callable) -> None:
        ...

    async def stop(self) -> None:
        ...
