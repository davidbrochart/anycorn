"""End-to-end WebSocket over HTTP/2, on a real TLS socket.

The extended CONNECT of RFC 8441 is what Chrome and Firefox use for every WebSocket
they open on an HTTP/2 connection, and it only exists over TLS in practice - ALPN is
how the connection became HTTP/2 in the first place. Driving it over a real socket,
with a real certificate from trustme, is the only way to know that the whole path
holds: ALPN, the SETTINGS that permit extended CONNECT at all, the CONNECT itself,
and WebSocket frames in DATA either way.

https://github.com/mattermost/mattermost/issues/30285 is what this looks like when
the server does not do it: Safari, which upgrades over HTTP/1.1, connects, while
Chrome and Firefox fail with a 1006.
"""

from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
import anyio.streams.tls
import h2.config
import h2.connection
import h2.events
import h2.settings
import pytest
import wsproto.connection
import wsproto.events

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.run import worker_serve

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from anyio.streams.tls import TLSStream

    from tests.conftest import TLSCerts

HOST = "127.0.0.1"


class _H2Client:
    """An HTTP/2 client over a real TLS stream, reading only as far as it is asked to."""

    def __init__(self, stream: TLSStream) -> None:
        self.stream = stream
        self.connection = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding=None)
        )
        self._events: list[h2.events.Event] = []

    async def initiate(self) -> None:
        self.connection.initiate_connection()
        await self.flush()

    async def flush(self) -> None:
        data = self.connection.data_to_send()
        if data:
            await self.stream.send(data)

    async def next_event(self, kind: type[h2.events.Event]) -> Any:  # noqa: ANN401
        """Read until an event of *kind* arrives, and return it.

        Events of other kinds are kept, so a caller asking for what comes later does
        not lose what arrived alongside what it asked for first.
        """
        while True:
            for index, event in enumerate(self._events):
                if isinstance(event, kind):
                    del self._events[index]
                    return event
            self._events.extend(self.connection.receive_data(await self.stream.receive()))
            await self.flush()


async def _serve_lifespan(receive: Any, send: Any) -> None:  # noqa: ANN401
    """Answer the lifespan protocol, so the server does not log its way past it."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def _client_context(tls_certs: TLSCerts) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(tls_certs.cafile))
    context.set_alpn_protocols(["h2"])
    return context


@asynccontextmanager
async def _serve(app: Callable, tls_certs: TLSCerts) -> AsyncIterator[TLSStream]:
    """Run a real server over TLS and yield a raw HTTP/2 connection to it."""
    config = Config()
    config.bind = [f"{HOST}:0"]  # OS-assigned; the bound URL comes back via task_status
    config.certfile = str(tls_certs.certfile)
    config.keyfile = str(tls_certs.keyfile)
    config.alpn_protocols = ["h2"]
    config.accesslog = "-"
    config.errorlog = "-"

    shutdown = anyio.Event()
    async with anyio.create_task_group() as task_group:
        binds: list[str] = await task_group.start(
            lambda *, task_status: worker_serve(
                ASGIWrapper(app), config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )
        port = urlsplit(binds[0]).port
        assert port is not None
        stream = await anyio.connect_tcp(
            HOST,
            port,
            ssl_context=_client_context(tls_certs),
            tls_hostname=HOST,
            # The server is torn down under the client, so a close_notify from it is
            # not something to wait on
            tls_standard_compatible=False,
        )
        try:
            yield stream
        finally:
            await stream.aclose()
            shutdown.set()


WEBSOCKET_CONNECT_HEADERS = [
    (b":method", b"CONNECT"),
    (b":protocol", b"websocket"),
    (b":scheme", b"https"),
    (b":authority", HOST.encode()),
    (b":path", b"/chat?a=b"),
    (b"sec-websocket-version", b"13"),
]


@pytest.mark.anyio
async def test_websocket_over_a_real_http2_tls_connection(tls_certs: TLSCerts) -> None:
    """A browser's WebSocket handshake, end to end over ALPN-negotiated HTTP/2."""
    scopes: list[dict] = []

    async def echo(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] == "lifespan":
            await _serve_lifespan(receive, send)
            return
        scopes.append(dict(scope))
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.receive":
                await send({"type": "websocket.send", "text": message["text"].upper()})
            else:
                return

    with anyio.fail_after(30):
        async with _serve(echo, tls_certs) as stream:
            assert stream.extra(anyio.streams.tls.TLSAttribute.alpn_protocol) == "h2"  # noqa: S610
            client = _H2Client(stream)
            await client.initiate()

            # Without this setting a browser will not send an extended CONNECT at all
            settings = await client.next_event(h2.events.RemoteSettingsChanged)
            enabled = settings.changed_settings[h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL]
            assert enabled.new_value == 1

            stream_id = client.connection.get_next_available_stream_id()
            client.connection.send_headers(stream_id, WEBSOCKET_CONNECT_HEADERS)
            await client.flush()

            response = await client.next_event(h2.events.ResponseReceived)
            assert dict(response.headers)[b":status"] == b"200"

            websocket = wsproto.connection.Connection(wsproto.connection.ConnectionType.CLIENT)
            client.connection.send_data(
                stream_id, websocket.send(wsproto.events.TextMessage(data="hello"))
            )
            await client.flush()

            data = await client.next_event(h2.events.DataReceived)
            websocket.receive_data(data.data)
            assert list(websocket.events()) == [
                wsproto.events.TextMessage(data="HELLO", frame_finished=True, message_finished=True)
            ]

            client.connection.send_data(
                stream_id, websocket.send(wsproto.events.CloseConnection(code=1000))
            )
            await client.flush()

            data = await client.next_event(h2.events.DataReceived)
            websocket.receive_data(data.data)
            assert list(websocket.events()) == [
                wsproto.events.CloseConnection(code=1000, reason="")
            ]
            await client.next_event(h2.events.StreamEnded)

    assert len(scopes) == 1
    scope = scopes[0]
    assert scope["type"] == "websocket"
    assert scope["http_version"] == "2"
    # wss, since the connection really is TLS - what the app checks to know the
    # WebSocket is secure
    assert scope["scheme"] == "wss"
    assert scope["path"] == "/chat"
    assert scope["query_string"] == b"a=b"
    assert "tls" in scope["extensions"]
    # RFC 8441 s5.1: none of the HTTP/1.1 handshake headers are used
    assert dict(scope["headers"]).keys() == {b"host", b"sec-websocket-version"}


@pytest.mark.anyio
async def test_connect_without_the_websocket_protocol_is_not_implemented(
    tls_certs: TLSCerts,
) -> None:
    """A plain tunnel CONNECT is refused, over the same real connection.

    Every CONNECT used to be served as a WebSocket, so this came back as the 400 of a
    malformed handshake from a server that is not a forward proxy at all.
    """
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] == "lifespan":
            await _serve_lifespan(receive, send)
            return
        seen.append(scope["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with anyio.fail_after(30):
        async with _serve(app, tls_certs) as stream:
            client = _H2Client(stream)
            await client.initiate()

            stream_id = client.connection.get_next_available_stream_id()
            client.connection.send_headers(
                stream_id,
                [
                    (b":method", b"CONNECT"),
                    (b":scheme", b"https"),
                    (b":authority", HOST.encode()),
                    (b":path", b"/chat"),
                ],
            )
            await client.flush()

            response = await client.next_event(h2.events.ResponseReceived)
            assert dict(response.headers)[b":status"] == b"501"

            # The connection is still good, rather than having been taken down with it
            stream_id = client.connection.get_next_available_stream_id()
            client.connection.send_headers(
                stream_id,
                [
                    (b":method", b"GET"),
                    (b":scheme", b"https"),
                    (b":authority", HOST.encode()),
                    (b":path", b"/after"),
                ],
                end_stream=True,
            )
            await client.flush()

            response = await client.next_event(h2.events.ResponseReceived)
            assert dict(response.headers)[b":status"] == b"200"

    assert seen == ["http"]
