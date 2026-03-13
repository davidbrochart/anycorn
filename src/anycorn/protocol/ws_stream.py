"""ASGI WebSocket stream handler that manages handshake and message framing."""

from __future__ import annotations

from enum import Enum, auto
from io import BytesIO, StringIO
from time import time
from typing import TYPE_CHECKING
from urllib.parse import unquote

from wsproto.connection import Connection, ConnectionState, ConnectionType
from wsproto.events import (
    BytesMessage,
    CloseConnection,
    Message,
    Ping,
    TextMessage,
)
from wsproto.events import (
    Event as WSProtoEvent,
)
from wsproto.extensions import Extension, PerMessageDeflate
from wsproto.frame_protocol import CloseReason
from wsproto.handshake import WEBSOCKET_VERSION, server_extensions_handshake
from wsproto.utilities import LocalProtocolError, generate_accept_token, split_comma_header

from anycorn.typing import (
    AppWrapper,
    ASGISendEvent,
    Extensions,
    TaskGroup,
    TLSExtension,
    WebsocketAcceptEvent,
    WebsocketResponseBodyEvent,
    WebsocketResponseStartEvent,
    WebsocketScope,
    WorkerContext,
)
from anycorn.utils import (
    UnexpectedMessageError,
    build_and_validate_headers,
    suppress_body,
    valid_server_name,
)

from .events import Body, Data, EndBody, EndData, Event, Request, Response, StreamClosed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from anycorn.config import Config


class ASGIWebsocketState(Enum):
    """State machine for ASGI WebSocket lifecycle.

    Hypercorn supports the ASGI websocket HTTP response extension,
    which allows HTTP responses rather than acceptance.
    """

    HANDSHAKE = auto()
    CONNECTED = auto()
    RESPONSE = auto()
    CLOSED = auto()
    HTTPCLOSED = auto()


class FrameTooLargeError(Exception):
    """Raised when a WebSocket message exceeds the configured maximum size."""



class Handshake:
    """Parses and validates a WebSocket upgrade handshake."""

    def __init__(self, headers: list[tuple[bytes, bytes]], http_version: str) -> None:
        """Parse WebSocket-related headers from the request."""
        self.accepted = False
        self.http_version = http_version
        self.connection_tokens: list[str] | None = None
        self.extensions: list[str] | None = None
        self.key: bytes | None = None
        self.subprotocols: list[str] | None = None
        self.upgrade: bytes | None = None
        self.version: bytes | None = None
        for raw_name, value in headers:
            name = raw_name.lower()
            if name == b"connection":
                self.connection_tokens = split_comma_header(value)
            elif name == b"sec-websocket-extensions":
                self.extensions = split_comma_header(value)
            elif name == b"sec-websocket-key":
                self.key = value
            elif name == b"sec-websocket-protocol":
                self.subprotocols = split_comma_header(value)
            elif name == b"sec-websocket-version":
                self.version = value
            elif name == b"upgrade":
                self.upgrade = value

    def is_valid(self) -> bool:
        """Return True if the handshake headers form a valid WebSocket upgrade."""
        if self.http_version < "1.1":
            return False
        if self.http_version == "1.1":
            if self.key is None:
                return False
            if self.connection_tokens is None or not any(
                token.lower() == "upgrade" for token in self.connection_tokens
            ):
                return False
            if self.upgrade.lower() != b"websocket":
                return False

        return self.version == WEBSOCKET_VERSION

    def accept(
        self,
        subprotocol: str | None,
        additional_headers: Iterable[tuple[bytes, bytes]],
    ) -> tuple[int, list[tuple[bytes, bytes]], Connection]:
        """Build accept headers and return the status code, headers, and connection."""
        headers = []
        if subprotocol is not None:
            if self.subprotocols is None or subprotocol not in self.subprotocols:
                msg = "Invalid Subprotocol"
                raise Exception(msg)  # noqa: TRY002
            headers.append((b"sec-websocket-protocol", subprotocol.encode()))

        extensions: list[Extension] = [PerMessageDeflate()]
        accepts = None
        if self.extensions is not None:
            accepts = server_extensions_handshake(self.extensions, extensions)

        if accepts:
            headers.append((b"sec-websocket-extensions", accepts))

        if self.key is not None:
            headers.append((b"sec-websocket-accept", generate_accept_token(self.key)))

        status_code = 200
        if self.http_version == "1.1":
            headers.extend([(b"upgrade", b"WebSocket"), (b"connection", b"Upgrade")])
            status_code = 101

        for name, value in additional_headers:
            if name == b"sec-websocket-protocol" or name.startswith(b":"):
                msg = f"Invalid additional header, {name.decode()}"
                raise Exception(msg)  # noqa: TRY002

            headers.append((name, value))

        self.accepted = True
        return status_code, headers, Connection(ConnectionType.SERVER, extensions)


class WebsocketBuffer:
    """Accumulates WebSocket message fragments into a single message."""

    def __init__(self, max_length: int) -> None:
        """Initialize with the maximum allowed message length in bytes."""
        self.value: BytesIO | StringIO | None = None
        self.length = 0
        self.max_length = max_length

    def extend(self, event: Message) -> None:
        """Append a message fragment, raising FrameTooLargeError if the limit is exceeded."""
        if self.value is None:
            if isinstance(event, TextMessage):
                self.value = StringIO()
            else:
                self.value = BytesIO()
        self.length += self.value.write(event.data)
        if self.length > self.max_length:
            raise FrameTooLargeError

    def clear(self) -> None:
        """Reset the buffer ready for the next message."""
        self.value = None
        self.length = 0

    def to_message(self) -> dict:
        """Return the accumulated data as a websocket.receive ASGI message."""
        return {
            "type": "websocket.receive",
            "bytes": self.value.getvalue() if isinstance(self.value, BytesIO) else None,
            "text": self.value.getvalue() if isinstance(self.value, StringIO) else None,
        }


class WSStream:
    """Handles a single WebSocket stream including handshake and message framing."""

    def __init__(  # noqa: PLR0913
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        task_group: TaskGroup,
        client: tuple[str, int] | None,
        server: tuple[str, int] | None,
        send: Callable[[Event], Awaitable[None]],
        stream_id: int,
        tls: TLSExtension | None,
    ) -> None:
        """Initialize the WebSocket stream handler."""
        self.app = app
        self.app_put: Callable | None = None
        self.buffer = WebsocketBuffer(config.websocket_max_message_size)
        self.client = client
        self.closed = False
        self.config = config
        self.context = context
        self.task_group = task_group
        self.response: WebsocketResponseStartEvent
        self.scope: WebsocketScope
        self.send = send
        self.server = server
        self.start_time: float
        self.state = ASGIWebsocketState.HANDSHAKE
        self.stream_id = stream_id
        self.tls = tls
        self.scheme = "wss" if self.tls is not None else "ws"

        self.connection: Connection
        self.handshake: Handshake

    @property
    def idle(self) -> bool:
        """Return True when the WebSocket stream is in a terminal state."""
        return self.state in {ASGIWebsocketState.CLOSED, ASGIWebsocketState.HTTPCLOSED}

    async def handle(self, event: Event) -> None:  # noqa: C901
        """Handle an incoming stream event."""
        if self.closed:
            return
        if isinstance(event, Request):
            self.start_time = time()
            self.handshake = Handshake(event.headers, event.http_version)
            path, _, query_string = event.raw_path.partition(b"?")
            extensions = Extensions()
            if self.tls is not None:
                extensions["tls"] = self.tls
            extensions["websocket.http.response"] = {}
            self.scope = {
                "type": "websocket",
                "asgi": {"spec_version": "2.3", "version": "3.0"},
                "scheme": self.scheme,
                "http_version": event.http_version,
                "path": unquote(path.decode("ascii")),
                "raw_path": path,
                "query_string": query_string,
                "root_path": self.config.root_path,
                "headers": event.headers,
                "client": self.client,
                "server": self.server,
                "state": event.state,
                "subprotocols": self.handshake.subprotocols or [],
                "extensions": extensions,
            }

            if not valid_server_name(self.config, event):
                await self._send_error_response(404)
                self.closed = True
            elif not self.handshake.is_valid():
                await self._send_error_response(400)
                self.closed = True
            else:
                self.app_put = await self.task_group.spawn_app(
                    self.app, self.config, self.scope, self.app_send
                )
                await self.app_put({"type": "websocket.connect"})
        elif isinstance(event, (Body, Data)) and not self.handshake.accepted:
            await self._send_error_response(400)
            self.closed = True
        elif isinstance(event, (Body, Data)):
            self.connection.receive_data(event.data)
            await self._handle_events()
        elif isinstance(event, StreamClosed):
            self.closed = True
            if self.app_put is not None:
                if self.state in {ASGIWebsocketState.HTTPCLOSED, ASGIWebsocketState.CLOSED}:
                    code = CloseReason.NORMAL_CLOSURE.value
                else:
                    code = CloseReason.ABNORMAL_CLOSURE.value
                await self.app_put({"type": "websocket.disconnect", "code": code})

    async def app_send(self, message: ASGISendEvent | None) -> None:  # noqa: C901, PLR0912
        """Handle a message sent by the ASGI application."""
        if self.closed:
            # Allow app to finish after close
            return

        if message is None:  # ASGI App has finished sending messages
            # Cleanup if required
            if self.state == ASGIWebsocketState.HANDSHAKE:
                await self._send_error_response(500)
                await self.config.log.access(
                    self.scope, {"status": 500, "headers": []}, time() - self.start_time
                )
            elif self.state == ASGIWebsocketState.CONNECTED:
                await self._send_wsproto_event(CloseConnection(code=CloseReason.INTERNAL_ERROR))
            await self.send(StreamClosed(stream_id=self.stream_id))
        elif message["type"] == "websocket.accept" and self.state == ASGIWebsocketState.HANDSHAKE:
            await self._accept(message)
        elif (
            message["type"] == "websocket.http.response.start"
            and self.state == ASGIWebsocketState.HANDSHAKE
        ):
            self.response = message
        elif message["type"] == "websocket.http.response.body" and self.state in {
            ASGIWebsocketState.HANDSHAKE,
            ASGIWebsocketState.RESPONSE,
        }:
            await self._send_rejection(message)
        elif message["type"] == "websocket.send" and self.state == ASGIWebsocketState.CONNECTED:
            event: WSProtoEvent
            if message.get("bytes") is not None:
                event = BytesMessage(data=bytes(message["bytes"]))
            elif not isinstance(message["text"], str):
                msg = f"{message['text']} should be a str"
                raise TypeError(msg)
            else:
                event = TextMessage(data=message["text"])
            await self._send_wsproto_event(event)
        elif (
            message["type"] == "websocket.close" and self.state == ASGIWebsocketState.HANDSHAKE
        ):
            self.state = ASGIWebsocketState.HTTPCLOSED
            await self._send_error_response(403)
        elif message["type"] == "websocket.close":
            self.state = ASGIWebsocketState.CLOSED
            await self._send_wsproto_event(
                CloseConnection(
                    code=int(message.get("code", CloseReason.NORMAL_CLOSURE)),
                    reason=message.get("reason"),
                )
            )
            await self.send(EndData(stream_id=self.stream_id))
        else:
            raise UnexpectedMessageError(self.state, message["type"])

    async def _handle_events(self) -> None:
        for event in self.connection.events():
            if isinstance(event, Message):
                try:
                    self.buffer.extend(event)
                except FrameTooLargeError:
                    await self._send_wsproto_event(
                        CloseConnection(code=CloseReason.MESSAGE_TOO_BIG)
                    )
                    break

                if event.message_finished:
                    await self.app_put(self.buffer.to_message())
                    self.buffer.clear()
            elif isinstance(event, Ping):
                await self._send_wsproto_event(event.response())
            elif isinstance(event, CloseConnection):
                if self.connection.state == ConnectionState.REMOTE_CLOSING:
                    await self._send_wsproto_event(event.response())
                await self.send(StreamClosed(stream_id=self.stream_id))

    async def _send_error_response(self, status_code: int) -> None:
        await self.send(
            Response(
                stream_id=self.stream_id,
                status_code=status_code,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
            )
        )
        await self.send(EndBody(stream_id=self.stream_id))
        await self.config.log.access(
            self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        )

    async def _send_wsproto_event(self, event: WSProtoEvent) -> None:
        try:
            data = self.connection.send(event)
        except LocalProtocolError:
            pass
        else:
            await self.send(Data(stream_id=self.stream_id, data=data))

    async def _accept(self, message: WebsocketAcceptEvent) -> None:
        self.state = ASGIWebsocketState.CONNECTED
        status_code, headers, self.connection = self.handshake.accept(
            message.get("subprotocol"), message.get("headers", [])
        )
        await self.send(
            Response(stream_id=self.stream_id, status_code=status_code, headers=headers)
        )
        await self.config.log.access(
            self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        )
        if self.config.websocket_ping_interval is not None:
            self.task_group.spawn(self._send_pings)

    async def _send_rejection(self, message: WebsocketResponseBodyEvent) -> None:
        body_suppressed = suppress_body("GET", self.response["status"])
        if self.state == ASGIWebsocketState.HANDSHAKE:
            headers = build_and_validate_headers(self.response["headers"])
            await self.send(
                Response(
                    stream_id=self.stream_id,
                    status_code=int(self.response["status"]),
                    headers=headers,
                )
            )
            self.state = ASGIWebsocketState.RESPONSE
        if not body_suppressed:
            await self.send(Body(stream_id=self.stream_id, data=bytes(message.get("body", b""))))
        if not message.get("more_body", False):
            self.state = ASGIWebsocketState.HTTPCLOSED
            await self.send(EndBody(stream_id=self.stream_id))
            await self.config.log.access(self.scope, self.response, time() - self.start_time)

    async def _send_pings(self) -> None:
        while not self.closed:
            await self._send_wsproto_event(Ping())
            await self.context.sleep(self.config.websocket_ping_interval)
