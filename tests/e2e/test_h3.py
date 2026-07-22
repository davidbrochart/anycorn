"""End-to-end HTTP/3 tests, driven by httpx2 over aioquic."""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted

import anycorn
from anycorn.config import Config
from anycorn.datagram import wrap_datagram_socket
from anycorn.protocol.h3 import H3Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aioquic.h3.events import H3Event

    from anycorn.datagram import DatagramSocket
    from tests.conftest import TLSCerts

HOST = "127.0.0.1"
# QUIC drives loss recovery off timers, so one has to be serviced even when no
# datagram arrives. Polling is coarser than the server's restartable timer task but
# far harder to get wrong, and on loopback the difference does not show.
TIMER_INTERVAL = 0.005


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"Hello, h3!"})


class _H3ResponseStream(httpx2.AsyncByteStream):
    def __init__(self, aiterator: AsyncIterator[bytes]) -> None:
        self._aiterator = aiterator

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._aiterator:
            yield part


class _H3Transport(httpx2.AsyncBaseTransport):
    """An httpx2 transport speaking HTTP/3, so a real client drives the server.

    aioquic ships one of these in its examples, but it subclasses
    `QuicConnectionProtocol` and so is asyncio only:
    https://github.com/aiortc/aioquic/blob/main/examples/httpx_client.py

    The request handling here follows that example. What differs is underneath it:
    aioquic's `QuicConnection` is sans-io, so this drives it over an anyio datagram
    socket exactly as `QuicProtocol` does server-side, which is what lets these tests
    run on trio as well as asyncio. The socket comes from `wrap_datagram_socket()`
    rather than anyio directly, so the client gets the same win32 asyncio treatment
    the server does - anyio's UDP `aclose()` otherwise hangs on the proactor loop.
    """

    def __init__(
        self, quic: QuicConnection, sock: DatagramSocket, address: tuple[str, int]
    ) -> None:
        self._quic = quic
        self._socket = sock
        self._address = address
        # Read now and kept: the socket is gone by the time a test asserts on it
        host, port = sock.socket.getsockname()[:2]
        self.local_address = (host, port)
        # Built now rather than once the handshake lands: the server's control and
        # QPACK encoder streams arrive with it, and anything received before this
        # exists is dropped - leaving later responses blocked on dynamic table
        # entries whose instructions were never seen
        self._http = H3Connection(quic)
        self._read_queue: dict[int, list[H3Event]] = {}
        self._read_ready: dict[int, anyio.Event] = {}
        self._connected = anyio.Event()
        self.handshakes = 0
        self._terminated = anyio.Event()
        self._send_lock = anyio.Lock()

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, host: str, port: int, certs: TLSCerts, local_port: int = 0
    ) -> AsyncIterator[_H3Transport]:
        """Open a QUIC connection, yielding a transport once the handshake lands."""
        configuration = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
        # Verify the chain rather than turning verification off: the handshake is
        # part of what these tests are covering
        configuration.load_verify_locations(cafile=str(certs.cafile))
        configuration.server_name = certs.hostname

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((HOST, local_port))
        sock.setblocking(False)  # noqa: FBT003
        datagram_socket = await wrap_datagram_socket(sock)

        quic = QuicConnection(configuration=configuration)
        self = cls(quic, datagram_socket, (host, port))
        try:
            async with anyio.create_task_group() as task_group:
                quic.connect(self._address, now=anyio.current_time())
                task_group.start_soon(self._receive_loop)
                task_group.start_soon(self._timer_loop)
                await self._transmit()

                with anyio.fail_after(10):
                    await self._connected.wait()
                await self._transmit()

                try:
                    yield self
                finally:
                    quic.close()
                    with anyio.CancelScope(shield=True):
                        await self._transmit()
                    task_group.cancel_scope.cancel()
        finally:
            await datagram_socket.aclose()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        stream_id = self._quic.get_next_available_stream_id()
        self._read_queue[stream_id] = []
        self._read_ready[stream_id] = anyio.Event()

        self._http.send_headers(
            stream_id=stream_id,
            headers=[
                (b":method", request.method.encode()),
                (b":scheme", request.url.raw_scheme),
                (b":authority", request.url.netloc),
                (b":path", request.url.raw_path),
                *[
                    (name.lower(), value)
                    for (name, value) in request.headers.raw
                    if name.lower() not in {b"connection", b"host"}
                ],
            ],
        )
        async for data in request.stream:  # type: ignore[union-attr]
            self._http.send_data(stream_id=stream_id, data=data, end_stream=False)
        self._http.send_data(stream_id=stream_id, data=b"", end_stream=True)
        await self._transmit()

        status_code, headers, stream_ended = await self._receive_response(stream_id)
        return httpx2.Response(
            status_code,
            headers=headers,
            stream=_H3ResponseStream(self._receive_response_data(stream_id, stream_ended)),
            extensions={"http_version": b"HTTP/3"},
        )

    async def _receive_loop(self) -> None:
        while True:
            try:
                data, address = await self._socket.receive()
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                return
            self._quic.receive_datagram(data, address, now=anyio.current_time())
            await self._process()

    async def _timer_loop(self) -> None:
        while True:
            await anyio.sleep(TIMER_INTERVAL)
            timer = self._quic.get_timer()
            if timer is not None and anyio.current_time() >= timer:
                self._quic.handle_timer(now=anyio.current_time())
                await self._process()

    async def _process(self) -> None:
        """Drain QUIC events into the HTTP layer, then flush whatever they produced."""
        event = self._quic.next_event()
        while event is not None:
            if isinstance(event, HandshakeCompleted):
                self.handshakes += 1
                self._connected.set()
            elif isinstance(event, ConnectionTerminated):
                self._terminated.set()
                for ready in self._read_ready.values():
                    ready.set()

            for h3_event in self._http.handle_event(event):
                self._queue_http_event(h3_event)

            event = self._quic.next_event()

        await self._transmit()

    def _queue_http_event(self, event: H3Event) -> None:
        if isinstance(event, (HeadersReceived, DataReceived)):
            queue = self._read_queue.get(event.stream_id)
            if queue is not None:
                queue.append(event)
                self._read_ready[event.stream_id].set()

    async def _transmit(self) -> None:
        # Sends come from the read loop and the timer as well as from requests, and
        # anyio permits one writer to a socket at a time
        async with self._send_lock:
            for data, address in self._quic.datagrams_to_send(now=anyio.current_time()):
                await self._socket.sendto(data, address[0], address[1])

    async def _receive_response(self, stream_id: int) -> tuple[int, list, bool]:
        while True:
            event = await self._wait_for_http_event(stream_id)
            if isinstance(event, HeadersReceived):
                break

        headers = []
        status_code = 0
        for header, value in event.headers:
            if header == b":status":
                status_code = int(value.decode())
            else:
                headers.append((header, value))
        return status_code, headers, event.stream_ended

    async def _receive_response_data(
        self,
        stream_id: int,
        stream_ended: bool,  # noqa: FBT001
    ) -> AsyncIterator[bytes]:
        while not stream_ended:
            event = await self._wait_for_http_event(stream_id)
            if isinstance(event, (DataReceived, HeadersReceived)):
                stream_ended = event.stream_ended
            if isinstance(event, DataReceived):
                yield event.data

    async def _wait_for_http_event(self, stream_id: int) -> H3Event:
        while not self._read_queue[stream_id]:
            if self._terminated.is_set():
                msg = "connection terminated before the response completed"
                raise AssertionError(msg)
            await self._read_ready[stream_id].wait()
        event = self._read_queue[stream_id].pop(0)
        if not self._read_queue[stream_id]:
            self._read_ready[stream_id] = anyio.Event()
        return event


@asynccontextmanager
async def _serving(
    certs: TLSCerts, tcp_port: int, quic_port: int, application: Callable = app
) -> AsyncIterator[None]:
    """Run the worker on the given ports, shutting it down on the way out."""
    config = Config()
    config.bind = [f"{HOST}:{tcp_port}"]
    config.quic_bind = [f"{HOST}:{quic_port}"]
    config.certfile = str(certs.certfile)
    config.keyfile = str(certs.keyfile)
    config.accesslog = "-"
    config.errorlog = "-"

    shutdown = anyio.Event()
    async with anyio.create_task_group() as task_group:
        await task_group.start(
            lambda *, task_status: anycorn.serve(
                application, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )
        try:
            yield
        finally:
            shutdown.set()


@pytest.mark.anyio
async def test_h3_request(tls_certs: TLSCerts, free_tcp_port: int, free_udp_port: int) -> None:
    async with (
        _serving(tls_certs, free_tcp_port, free_udp_port),
        _H3Transport.connect(HOST, free_udp_port, tls_certs) as transport,
        httpx2.AsyncClient(transport=transport) as client,
    ):
        with anyio.fail_after(10):
            response = await client.get(f"https://{HOST}:{free_udp_port}/")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.text == "Hello, h3!"
    assert response.headers["content-type"] == "text/plain"
    assert response.extensions["http_version"] == b"HTTP/3"


@pytest.mark.anyio
async def test_stream_closed_forgets_the_stream(
    tls_certs: TLSCerts,
    free_tcp_port: int,
    free_udp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H3Protocol must forget a stream once it closes, not accumulate it forever.

    stream_send's StreamClosed handling used to be `pass  # ??`, silently leaving
    every finished stream's HTTPStream/WSStream sitting in self.streams for the
    life of the QUIC connection - an unbounded leak on any connection that outlives
    more than a handful of requests. This drives a real HTTP/3 request through a
    real running server and inspects the actual H3Protocol instance's own streams
    dict afterward, rather than constructing one directly and calling stream_send
    by hand.
    """
    captured: list[H3Protocol] = []
    original_init = H3Protocol.__init__

    def _capturing_init(self: H3Protocol, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original_init(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(H3Protocol, "__init__", _capturing_init)

    async with (
        _serving(tls_certs, free_tcp_port, free_udp_port),
        _H3Transport.connect(HOST, free_udp_port, tls_certs) as transport,
        httpx2.AsyncClient(transport=transport) as client,
    ):
        with anyio.fail_after(10):
            response = await client.get(f"https://{HOST}:{free_udp_port}/")
        assert response.status_code == 200  # noqa: PLR2004

        assert len(captured) == 1
        # StreamClosed is sent after the response body, so the client seeing the
        # full response does not guarantee the server has processed it yet
        with anyio.fail_after(10):
            while True:
                if not captured[0].streams:
                    break
                await anyio.sleep(0.01)

    assert captured[0].streams == {}


CONCURRENT_REQUESTS = 3


def _rendezvous_app(
    arrived: list[str],
    clients: list[tuple[str, int] | None],
    states: list[dict],
    released: anyio.Event,
) -> Callable:
    """Return an app that answers nothing until every request has arrived.

    Which only completes if the server is carrying them at once. Were it taking them
    one at a time - a connection per request, or a stream at a time - the first would
    sit waiting for siblings that cannot be read yet, and the test would time out.
    """

    async def _app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
        assert scope["type"] == "http"
        arrived.append(scope["path"])
        clients.append(scope["client"])
        # Held rather than recorded by id: a freed namespace's address can be handed
        # straight to the next one, so ids only tell them apart whilst all are alive
        states.append(scope["state"])
        scope["state"]["secret"] = scope["path"]
        if len(arrived) == CONCURRENT_REQUESTS:
            released.set()
        await released.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": scope["path"].encode()})

    return _app


@pytest.mark.anyio
async def test_concurrent_requests_are_multiplexed(
    tls_certs: TLSCerts, free_tcp_port: int, free_udp_port: int
) -> None:
    """The server carries concurrent HTTP/3 requests at once, on one connection.

    That there is one connection is the harness's doing - a transport wraps a single
    QuicConnection on a single socket - so it is the fixed condition here rather than
    the finding. What is being tested is that the server carries three requests over
    it simultaneously instead of taking them in turn.
    """
    arrived: list[str] = []
    clients: list[tuple[str, int] | None] = []
    states: list[dict] = []
    released = anyio.Event()
    responses: dict[str, str] = {}

    async with (
        _serving(
            tls_certs,
            free_tcp_port,
            free_udp_port,
            _rendezvous_app(arrived, clients, states, released),
        ),
        _H3Transport.connect(HOST, free_udp_port, tls_certs) as transport,
        httpx2.AsyncClient(transport=transport) as client,
    ):

        async def _request(path: str) -> None:
            response = await client.get(f"https://{HOST}:{free_udp_port}{path}")
            assert response.status_code == 200  # noqa: PLR2004
            responses[path] = response.text

        paths = [f"/{index}" for index in range(CONCURRENT_REQUESTS)]
        with anyio.fail_after(10):
            async with anyio.create_task_group() as task_group:
                for path in paths:
                    task_group.start_soon(_request, path)

    # Every request answered, and answered as itself rather than as a sibling
    assert responses == {path: path for path in paths}
    assert sorted(arrived) == sorted(paths)
    # Sharing the connection is not sharing a namespace: each was handed its own, and
    # all three were in flight together, so any sharing would have been visible
    assert len({id(state) for state in states}) == CONCURRENT_REQUESTS
    assert [state["secret"] for state in states] == arrived

    # The conditions the rendezvous was met under: one socket, one handshake, so the
    # three were carried together over one connection rather than spread across three
    assert clients == [transport.local_address] * CONCURRENT_REQUESTS
    assert transport.handshakes == 1


def _state_app(seen: list[tuple[str, dict, int, tuple[str, int] | None]]) -> Callable:
    """Return an app that records the state it was handed, then writes to it."""

    async def _app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
        state = scope["state"]
        seen.append((scope["path"], dict(state), id(state), scope["client"]))
        state["secret"] = scope["path"]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return _app


@pytest.mark.anyio
async def test_state_is_not_shared_between_connections(
    tls_certs: TLSCerts,
    free_tcp_port: int,
    free_udp_port: int,
    free_udp_port_factory: Callable[[], int],
) -> None:
    """One client cannot read what the last one left behind.

    Both connections are made from the same client port, so the server sees one
    address throughout and has nothing to tell them apart by except the connection
    itself. Copying per request is what enforces this now; the per-connection copy
    behind it is defence in depth, so this asserts the guarantee rather than either
    layer.
    """
    seen: list[tuple[str, dict, int, tuple[str, int] | None]] = []
    client_port = free_udp_port_factory()

    async with _serving(tls_certs, free_tcp_port, free_udp_port, _state_app(seen)):
        for path in ("/first", "/second"):
            async with (
                _H3Transport.connect(
                    HOST, free_udp_port, tls_certs, local_port=client_port
                ) as transport,
                httpx2.AsyncClient(transport=transport) as client,
            ):
                assert transport.local_address == (HOST, client_port)
                with anyio.fail_after(10):
                    response = await client.get(f"https://{HOST}:{free_udp_port}{path}")
                assert response.status_code == 200  # noqa: PLR2004

    assert [path for path, _, _, _ in seen] == ["/first", "/second"]
    # The same peer both times, so the server had no address to tell them apart by
    assert [client for _, _, _, client in seen] == [(HOST, client_port)] * 2
    # And still neither the first's contents nor its namespace
    assert [state for _, state, _, _ in seen] == [{}, {}]
    first_id, second_id = (ident for _, _, ident, _ in seen)
    assert first_id != second_id
