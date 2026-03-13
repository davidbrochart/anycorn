"""Type definitions for the Anycorn ASGI/WSGI server."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from multiprocessing.synchronize import Event as EventType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    NewType,
    Protocol,
    TypedDict,
)

import h11

from .config import Config, Sockets

if TYPE_CHECKING:
    from types import TracebackType

    import h2.events

if sys.version_info >= (3, 11):
    from typing import NotRequired, Self
else:
    from typing_extensions import NotRequired, Self

H11SendableEvent = h11.Data | h11.EndOfMessage | h11.InformationalResponse | h11.Response

WorkerFunc = Callable[[Config, Sockets | None, EventType | None], None]

LifespanState = dict[str, Any]

ConnectionState = NewType("ConnectionState", dict[str, Any])


class ASGIVersions(TypedDict, total=False):
    """ASGI version information."""

    spec_version: str
    version: Literal["2.0", "3.0"]


class TLSExtension(TypedDict, total=False):
    """TLS connection extension data for the ASGI connection scope."""

    server_cert: str | None
    client_cert_chain: Sequence[str]
    client_cert_name: str | None
    client_cert_error: str | None
    tls_version: int | None
    cipher_suite: int | None


Extensions = TypedDict(
    "Extensions",
    {
        "tls": TLSExtension,
        "http.response.push": Mapping[str, Any],
        "http.response.trailers": Mapping[str, Any],
        "http.response.early_hint": Mapping[str, Any],
        "websocket.http.response": Mapping[str, Any],
    },
    total=False,
)


class HTTPScope(TypedDict):
    """ASGI HTTP connection scope."""

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
    extensions: Extensions


class WebsocketScope(TypedDict):
    """ASGI WebSocket connection scope."""

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
    extensions: Extensions


class LifespanScope(TypedDict):
    """ASGI Lifespan connection scope."""

    type: Literal["lifespan"]
    asgi: ASGIVersions
    state: LifespanState


WWWScope = HTTPScope | WebsocketScope
Scope = HTTPScope | WebsocketScope | LifespanScope


class HTTPRequestEvent(TypedDict):
    """ASGI HTTP request receive event."""

    type: Literal["http.request"]
    body: bytes
    more_body: bool


class HTTPResponseStartEvent(TypedDict):
    """ASGI HTTP response start send event."""

    type: Literal["http.response.start"]
    status: int
    headers: Iterable[tuple[bytes, bytes]]
    trailers: NotRequired[bool]


class HTTPResponseBodyEvent(TypedDict):
    """ASGI HTTP response body send event."""

    type: Literal["http.response.body"]
    body: bytes
    more_body: bool


class HTTPResponseTrailersEvent(TypedDict):
    """ASGI HTTP response trailers send event."""

    type: Literal["http.response.trailers"]
    headers: Iterable[tuple[bytes, bytes]]
    more_trailers: NotRequired[bool]


class HTTPServerPushEvent(TypedDict):
    """ASGI HTTP server push send event."""

    type: Literal["http.response.push"]
    path: str
    headers: Iterable[tuple[bytes, bytes]]


class HTTPEarlyHintEvent(TypedDict):
    """ASGI HTTP early hint send event."""

    type: Literal["http.response.early_hint"]
    links: Iterable[bytes]


class HTTPDisconnectEvent(TypedDict):
    """ASGI HTTP disconnect receive event."""

    type: Literal["http.disconnect"]


class WebsocketConnectEvent(TypedDict):
    """ASGI WebSocket connect receive event."""

    type: Literal["websocket.connect"]


class WebsocketAcceptEvent(TypedDict):
    """ASGI WebSocket accept send event."""

    type: Literal["websocket.accept"]
    subprotocol: str | None
    headers: Iterable[tuple[bytes, bytes]]


class WebsocketReceiveEvent(TypedDict):
    """ASGI WebSocket receive event."""

    type: Literal["websocket.receive"]
    bytes: bytes | None
    text: str | None


class WebsocketSendEvent(TypedDict):
    """ASGI WebSocket send event."""

    type: Literal["websocket.send"]
    bytes: bytes | None
    text: str | None


class WebsocketResponseStartEvent(TypedDict):
    """ASGI WebSocket HTTP response start event."""

    type: Literal["websocket.http.response.start"]
    status: int
    headers: Iterable[tuple[bytes, bytes]]


class WebsocketResponseBodyEvent(TypedDict):
    """ASGI WebSocket HTTP response body event."""

    type: Literal["websocket.http.response.body"]
    body: bytes
    more_body: bool


class WebsocketDisconnectEvent(TypedDict):
    """ASGI WebSocket disconnect receive event."""

    type: Literal["websocket.disconnect"]
    code: int


class WebsocketCloseEvent(TypedDict):
    """ASGI WebSocket close send event."""

    type: Literal["websocket.close"]
    code: int
    reason: str | None


class LifespanStartupEvent(TypedDict):
    """ASGI Lifespan startup receive event."""

    type: Literal["lifespan.startup"]


class LifespanShutdownEvent(TypedDict):
    """ASGI Lifespan shutdown receive event."""

    type: Literal["lifespan.shutdown"]


class LifespanStartupCompleteEvent(TypedDict):
    """ASGI Lifespan startup complete send event."""

    type: Literal["lifespan.startup.complete"]


class LifespanStartupFailedEvent(TypedDict):
    """ASGI Lifespan startup failed send event."""

    type: Literal["lifespan.startup.failed"]
    message: str


class LifespanShutdownCompleteEvent(TypedDict):
    """ASGI Lifespan shutdown complete send event."""

    type: Literal["lifespan.shutdown.complete"]


class LifespanShutdownFailedEvent(TypedDict):
    """ASGI Lifespan shutdown failed send event."""

    type: Literal["lifespan.shutdown.failed"]
    message: str


ASGIReceiveEvent = (
    HTTPRequestEvent
    | HTTPDisconnectEvent
    | WebsocketConnectEvent
    | WebsocketReceiveEvent
    | WebsocketDisconnectEvent
    | LifespanStartupEvent
    | LifespanShutdownEvent
)


ASGISendEvent = (
    HTTPResponseStartEvent
    | HTTPResponseBodyEvent
    | HTTPResponseTrailersEvent
    | HTTPServerPushEvent
    | HTTPEarlyHintEvent
    | HTTPDisconnectEvent
    | WebsocketAcceptEvent
    | WebsocketSendEvent
    | WebsocketResponseStartEvent
    | WebsocketResponseBodyEvent
    | WebsocketCloseEvent
    | LifespanStartupCompleteEvent
    | LifespanStartupFailedEvent
    | LifespanShutdownCompleteEvent
    | LifespanShutdownFailedEvent
)


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
Framework = ASGIFramework | WSGIFramework


class H2SyncStream(Protocol):
    """Protocol for synchronous HTTP/2 stream handling."""

    scope: dict

    def data_received(self, data: bytes) -> None:
        """Handle received data for this stream."""
        ...

    def ended(self) -> None:
        """Signal that the stream has ended."""
        ...

    def reset(self) -> None:
        """Reset the stream."""
        ...

    def close(self) -> None:
        """Close the stream."""
        ...

    async def handle_request(
        self,
        event: h2.events.RequestReceived,
        scheme: str,
        client: tuple[str, int],
        server: tuple[str, int],
    ) -> None:
        """Handle an incoming HTTP/2 request event."""
        ...


class H2AsyncStream(Protocol):
    """Protocol for asynchronous HTTP/2 stream handling."""

    scope: dict

    async def data_received(self, data: bytes) -> None:
        """Handle received data for this stream."""
        ...

    async def ended(self) -> None:
        """Signal that the stream has ended."""
        ...

    async def reset(self) -> None:
        """Reset the stream."""
        ...

    async def close(self) -> None:
        """Close the stream."""
        ...

    async def handle_request(
        self,
        event: h2.events.RequestReceived,
        scheme: str,
        client: tuple[str, int],
        server: tuple[str, int],
    ) -> None:
        """Handle an incoming HTTP/2 request event."""
        ...


class Event(Protocol):
    """Protocol for async event primitives."""

    def __init__(self) -> None: ...

    async def clear(self) -> None:
        """Clear the event, resetting it to the unset state."""
        ...

    async def set(self) -> None:
        """Set the event, unblocking any waiters."""
        ...

    async def wait(self) -> None:
        """Wait until the event is set."""
        ...

    def is_set(self) -> bool:
        """Return True if the event is currently set."""
        ...


class WorkerContext(Protocol):
    """Protocol describing the shared state passed to each connection handler."""

    event_class: ClassVar[type[Event]]
    single_task_class: ClassVar[type[SingleTask]]
    terminate: Event
    terminated: Event

    async def mark_request(self) -> None:
        """Record that a request has been received."""
        ...

    @staticmethod
    async def sleep(wait: float) -> None:
        """Sleep for the given number of seconds."""
        ...

    @staticmethod
    def time() -> float:
        """Return the current time as a float."""
        ...


class TaskGroup(Protocol):
    """Protocol for the task group used to manage concurrent ASGI tasks."""

    async def spawn_app(
        self,
        app: AppWrapper,
        config: Config,
        scope: Scope,
        send: Callable[[ASGISendEvent | None], Awaitable[None]],
    ) -> Callable[[ASGIReceiveEvent], Awaitable[None]]:
        """Spawn the ASGI app as a task and return a callable to send receive events."""
        ...

    def spawn(self, func: Callable, *args: Any) -> None:  # noqa: ANN401
        """Spawn a background task running func with the given arguments."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class ResponseSummary(TypedDict):
    """Minimal HTTP response summary used for access logging."""

    status: int
    headers: Iterable[tuple[bytes, bytes]]


class AppWrapper(Protocol):
    """Protocol for application wrappers (ASGI or WSGI)."""

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
        call_soon: Callable,
    ) -> None:
        """Invoke the wrapped application."""
        ...


class SingleTask(Protocol):
    """Protocol for managing a single restartable background task."""

    def __init__(self) -> None: ...

    async def restart(self, task_group: TaskGroup, action: Callable) -> None:
        """Restart the background task using the given task group and action callable."""
        ...

    async def stop(self) -> None:
        """Stop the background task."""
        ...
