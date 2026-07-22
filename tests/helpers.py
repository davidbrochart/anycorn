"""Test helper utilities and mock classes for anycorn tests."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from json import dumps
from math import inf
from socket import AF_INET
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.abc
from anyio.abc import SocketAttribute
from anyio.streams.tls import TLSAttribute

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.tcp_server import TCPServer
from anycorn.worker_context import WorkerContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope, WWWScope

SANITY_BODY = b"Hello Anycorn"


class MockSocket:
    """Mock socket for testing network connections."""

    family = AF_INET

    def getsockname(self) -> tuple[str, int]:
        return ("162.1.1.1", 80)

    def getpeername(self) -> tuple[str, int]:
        return ("127.0.0.1", 80)


class MemorySocketStream(anyio.abc.SocketStream):
    """An in-memory stand-in for a connected socket, to drive `TCPServer` directly.

    Replaces trio's `memory_stream_pair()`, which the tests these support were
    originally written against. `TCPServer` reads typed attributes off its stream -
    the raw socket always, and the ALPN protocol to decide between h11 and h2 - so a
    bare pair of byte streams will not do; those are supplied by `attributes`.
    """

    def __init__(
        self,
        receive_stream: MemoryObjectReceiveStream[bytes],
        send_stream: MemoryObjectSendStream[bytes],
        attributes: Mapping[Any, Callable[[], Any]],
    ) -> None:
        self._receive_stream = receive_stream
        self._send_stream = send_stream
        self._attributes = attributes
        self._buffer = bytearray()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._attributes

    async def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._buffer:
            # Propagates anyio.EndOfStream once the peer has closed, which is what
            # TCPServer treats as the connection going away
            self._buffer.extend(await self._receive_stream.receive())
        data = bytes(self._buffer[:max_bytes])
        del self._buffer[:max_bytes]
        return data

    async def send(self, item: bytes) -> None:
        if not item:
            # Writing nothing is a no-op on a socket, not an end of stream
            return
        await self._send_stream.send(item)

    async def send_eof(self) -> None:
        self._send_stream.close()

    async def aclose(self) -> None:
        self._send_stream.close()
        self._receive_stream.close()


class MemoryClientStream(anyio.abc.AsyncResource):
    """The client end of `memory_socket_stream_pair()`.

    Named for trio's stream API rather than anyio's, because that is what reads
    naturally in the tests: bytes in, bytes out, with EOF as an empty read.
    """

    def __init__(
        self,
        receive_stream: MemoryObjectReceiveStream[bytes],
        send_stream: MemoryObjectSendStream[bytes],
    ) -> None:
        self._receive_stream = receive_stream
        self._send_stream = send_stream
        self._buffer = bytearray()

    async def send_all(self, data: bytes) -> None:
        if not data:
            # A protocol library with nothing queued hands back b"", and writing that
            # to a socket does nothing. Passing it on would instead surface to the
            # server as the empty read that means the peer has gone away.
            return
        await self._send_stream.send(data)

    async def receive_some(self, max_bytes: int = 65536) -> bytes:
        if not self._buffer:
            try:
                self._buffer.extend(await self._receive_stream.receive())
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                # An empty read is how a closed connection reads on a real socket
                return b""
        data = bytes(self._buffer[:max_bytes])
        del self._buffer[:max_bytes]
        return data

    async def aclose(self) -> None:
        self._send_stream.close()
        self._receive_stream.close()


def memory_socket_stream_pair(
    *, alpn_protocol: str | None = None
) -> tuple[MemoryClientStream, MemorySocketStream]:
    """Return connected client and server stream ends, as a real socket pair would be.

    Pass `alpn_protocol="h2"` to have `TCPServer` negotiate HTTP/2; leaving it unset
    presents the stream as plain TCP, which is what makes `TCPServer` fall back to
    h11. The buffers are unbounded so that neither end has to be reading for the other
    to make progress, matching the trio memory streams these replace.

    Prefer `serve_in_memory()`, which owns closing both ends.
    """
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream[bytes](inf)
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream[bytes](inf)

    attributes: dict[Any, Callable[[], Any]] = {SocketAttribute.raw_socket: MockSocket}
    if alpn_protocol is not None:
        attributes[TLSAttribute.alpn_protocol] = lambda: alpn_protocol

    return (
        MemoryClientStream(server_to_client_receive, client_to_server_send),
        MemorySocketStream(client_to_server_receive, server_to_client_send, attributes),
    )


@asynccontextmanager
async def serve_in_memory(
    app: Callable,
    config: Config | None = None,
    *,
    alpn_protocol: str | None = None,
) -> AsyncIterator[MemoryClientStream]:
    """Run a `TCPServer` against an in-memory socket, yielding the client end.

    The context manager owns two things the tests must not be left to get right. The
    server task is cancelled on the way out, because a connection the client simply
    stops talking on - a websocket, or an h2 stream left open - is one the server is
    entitled to wait on forever. And every memory stream is closed, since an unclosed
    one surfaces as a `ResourceWarning` that this suite turns into a failure, from
    whichever test the garbage collector happens to run in.
    """
    client_stream, server_stream = memory_socket_stream_pair(alpn_protocol=alpn_protocol)
    server = TCPServer(
        ASGIWrapper(app),
        config if config is not None else Config(),
        WorkerContext(None),
        {},
        server_stream,
    )
    async with AsyncExitStack() as stack:
        # Closed last, once the server has stopped touching them
        await stack.enter_async_context(client_stream)
        await stack.enter_async_context(server_stream)

        task_group = await stack.enter_async_context(anyio.create_task_group())
        # Runs before the task group is waited on, so leaving the body stops the
        # server rather than waiting on a connection it may never see the end of
        stack.callback(task_group.cancel_scope.cancel)
        task_group.start_soon(server.run)

        yield client_stream


async def empty_framework(scope: Scope, receive: Callable, send: Callable) -> None:
    pass


class SlowLifespanFramework:
    """ASGI framework that sleeps during startup to simulate slow lifespan."""

    def __init__(self, delay: float, sleep: Callable) -> None:
        self.delay = delay
        self.sleep = sleep

    async def __call__(
        self,
        _scope: Scope,
        _receive: ASGIReceiveCallable,
        _send: ASGISendCallable,
    ) -> None:
        await self.sleep(self.delay)


async def echo_framework(
    input_scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    input_scope = cast("WWWScope", input_scope)
    scope = deepcopy(input_scope)
    scope["query_string"] = scope["query_string"].decode()  # type: ignore[arg-type]
    scope["raw_path"] = scope["raw_path"].decode()  # type: ignore[arg-type]
    scope["headers"] = [  # type: ignore[invalid-assignment]
        (name.decode(), value.decode()) for name, value in scope["headers"]
    ]

    body = bytearray()
    while True:
        event = await receive()
        if event["type"] in {"http.disconnect", "websocket.disconnect"}:
            break
        if event["type"] == "http.request":
            body.extend(event.get("body", b""))
            if not event.get("more_body", False):
                response = dumps({"scope": scope, "request_body": body.decode()}).encode()
                content_length = len(response)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", str(content_length).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": response, "more_body": False})
                break
        elif event["type"] == "websocket.connect":
            await send({"type": "websocket.accept"})  # type: ignore[misc, arg-type]
        elif event["type"] == "websocket.receive":
            await send({"type": "websocket.send", "text": event["text"], "bytes": event["bytes"]})


async def sanity_framework(
    scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    body = b""
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})  # type: ignore[misc, arg-type]

    while True:
        event = await receive()
        if event["type"] in {"http.disconnect", "websocket.disconnect"}:
            break
        if event["type"] == "lifespan.startup":
            assert "state" in scope
            await send({"type": "lifspan.startup.complete"})  # type: ignore[misc, arg-type]
        elif event["type"] == "lifespan.shutdown":
            await send({"type": "lifspan.shutdown.complete"})  # type: ignore[misc, arg-type]
        elif event["type"] == "http.request" and event.get("more_body", False):
            body += event["body"]
        elif event["type"] == "http.request" and not event.get("more_body", False):
            body += event["body"]
            assert body == SANITY_BODY
            response = b"Hello & Goodbye"
            content_length = len(response)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(content_length).encode())],
                }
            )
            await send({"type": "http.response.body", "body": response, "more_body": False})
            break
        elif event["type"] == "websocket.receive":
            assert event["bytes"] == SANITY_BODY
            await send({"type": "websocket.send", "text": "Hello & Goodbye"})  # type: ignore[arg-type]
