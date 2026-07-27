"""Sanity tests for Anycorn.

These drive a real `TCPServer` over an in-memory socket, speaking the wire protocol
from the client side, so they cover the whole stack from bytes to ASGI and back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio
import h2.config
import h2.connection
import h2.events
import h11
import pytest
import wsproto
import wsproto.connection
import wsproto.events

from anycorn.config import Config

from .helpers import SANITY_BODY, sanity_framework, serve_in_memory

if TYPE_CHECKING:
    from collections.abc import Callable

    from anycorn.typing import HTTPScope, Scope


@pytest.mark.anyio
async def test_http1_request() -> None:
    async with serve_in_memory(sanity_framework) as client_stream:
        client = h11.Connection(h11.CLIENT)
        await client_stream.send_all(
            client.send(
                h11.Request(
                    method="POST",
                    target="/",
                    headers=[
                        (b"host", b"anycorn"),
                        (b"connection", b"close"),
                        (b"content-length", b"%d" % len(SANITY_BODY)),
                    ],
                )
            )
        )
        await client_stream.send_all(client.send(h11.Data(data=SANITY_BODY)))
        await client_stream.send_all(client.send(h11.EndOfMessage()))

        events = []
        while True:
            event = client.next_event()
            if event == h11.NEED_DATA:
                client.receive_data(await client_stream.receive_some(1024))
            elif isinstance(event, h11.ConnectionClosed):
                break
            else:
                events.append(event)

    assert events == [
        h11.Response(
            status_code=200,
            headers=[
                (b"content-length", b"15"),
                (b"date", b"Thu, 01 Jan 1970 01:23:20 GMT"),
                (b"server", b"anycorn-h11"),
                (b"connection", b"close"),
            ],
            http_version=b"1.1",
            reason=b"",
        ),
        h11.Data(data=b"Hello & Goodbye"),
        h11.EndOfMessage(headers=[]),
    ]


@pytest.mark.anyio
async def test_http1_websocket() -> None:
    async with serve_in_memory(sanity_framework) as client_stream:
        client = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
        await client_stream.send_all(
            client.send(wsproto.events.Request(host="anycorn", target="/"))
        )
        client.receive_data(await client_stream.receive_some(1024))
        assert list(client.events()) == [
            wsproto.events.AcceptConnection(
                extra_headers=[
                    (b"date", b"Thu, 01 Jan 1970 01:23:20 GMT"),
                    (b"server", b"anycorn-h11"),
                ]
            )
        ]

        await client_stream.send_all(client.send(wsproto.events.BytesMessage(data=SANITY_BODY)))
        client.receive_data(await client_stream.receive_some(1024))
        assert list(client.events()) == [wsproto.events.TextMessage(data="Hello & Goodbye")]

        await client_stream.send_all(client.send(wsproto.events.CloseConnection(code=1000)))
        client.receive_data(await client_stream.receive_some(1024))
        assert list(client.events()) == [wsproto.events.CloseConnection(code=1000, reason="")]


@pytest.mark.anyio
async def test_http2_request() -> None:
    async with serve_in_memory(sanity_framework, alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())
        stream_id = client.get_next_available_stream_id()
        client.send_headers(
            stream_id,
            [
                (":method", "GET"),
                (":path", "/"),
                (":authority", "anycorn"),
                (":scheme", "https"),
                ("content-length", str(len(SANITY_BODY))),
            ],
        )
        client.send_data(stream_id, SANITY_BODY)
        client.end_stream(stream_id)
        await client_stream.send_all(client.data_to_send())

        events = []
        open_ = True
        while open_:
            data = await client_stream.receive_some(1024)
            if data == b"":
                break
            for event in client.receive_data(data):
                if isinstance(event, h2.events.DataReceived):
                    client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(
                    event,
                    (h2.events.ConnectionTerminated, h2.events.StreamEnded, h2.events.StreamReset),
                ):
                    open_ = False
                    break
                else:
                    events.append(event)
            await client_stream.send_all(client.data_to_send())

    assert isinstance(events[2], h2.events.ResponseReceived)
    assert events[2].headers == [
        (b":status", b"200"),
        (b"content-length", b"15"),
        (b"date", b"Thu, 01 Jan 1970 01:23:20 GMT"),
        (b"server", b"anycorn-h2"),
    ]


@pytest.mark.anyio
async def test_http2_websocket() -> None:
    async with serve_in_memory(sanity_framework, alpn_protocol="h2") as client_stream:
        h2_client = h2.connection.H2Connection()
        h2_client.initiate_connection()
        await client_stream.send_all(h2_client.data_to_send())
        stream_id = h2_client.get_next_available_stream_id()
        h2_client.send_headers(
            stream_id,
            [
                (":method", "CONNECT"),
                (":path", "/"),
                (":authority", "anycorn"),
                (":scheme", "https"),
                ("sec-websocket-version", "13"),
            ],
        )
        await client_stream.send_all(h2_client.data_to_send())

        events = h2_client.receive_data(await client_stream.receive_some(1024))
        await client_stream.send_all(h2_client.data_to_send())
        events = h2_client.receive_data(await client_stream.receive_some(1024))
        while not isinstance(events[-1], h2.events.ResponseReceived):
            events = h2_client.receive_data(await client_stream.receive_some(1024))
        assert events[-1].headers == [
            (b":status", b"200"),
            (b"date", b"Thu, 01 Jan 1970 01:23:20 GMT"),
            (b"server", b"anycorn-h2"),
        ]

        client = wsproto.connection.Connection(wsproto.ConnectionType.CLIENT)
        h2_client.send_data(stream_id, client.send(wsproto.events.BytesMessage(data=SANITY_BODY)))
        await client_stream.send_all(h2_client.data_to_send())
        events = h2_client.receive_data(await client_stream.receive_some(1024))
        assert isinstance(events[0], h2.events.DataReceived)
        client.receive_data(events[0].data)
        assert list(client.events()) == [wsproto.events.TextMessage(data="Hello & Goodbye")]

        h2_client.send_data(stream_id, client.send(wsproto.events.CloseConnection(code=1000)))
        await client_stream.send_all(h2_client.data_to_send())
        events = h2_client.receive_data(await client_stream.receive_some(1024))
        assert isinstance(events[0], h2.events.DataReceived)
        client.receive_data(events[0].data)
        assert list(client.events()) == [wsproto.events.CloseConnection(code=1000, reason="")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("target", "status"),
    [
        (b"/bad%ff", 400),  # a valid escape for a byte that is not UTF-8
        (b"/x%zz", 400),  # not a hex escape at all
    ],
)
async def test_http1_error_response_closes_the_connection(target: bytes, status: int) -> None:
    """A request answered with an error must finish, not leave the client waiting.

    The error response was written but the stream was never closed, so h11 never
    reached _maybe_recycle and the connection stayed open until it timed out - the
    client saw the response and then simply hung. Only reachable once anycorn
    itself started refusing requests h11 had already let through.
    """
    config = Config()
    config.server_names = ["anycorn"]
    async with serve_in_memory(sanity_framework, config) as client_stream:
        client = h11.Connection(h11.CLIENT)
        await client_stream.send_all(
            client.send(
                h11.Request(
                    method="GET",
                    target=target,
                    headers=[(b"host", b"anycorn"), (b"connection", b"close")],
                )
            )
        )

        events = []
        with anyio.fail_after(5):
            while True:
                event = client.next_event()
                if event is h11.NEED_DATA:
                    client.receive_data(await client_stream.receive_some(4096))
                elif isinstance(event, h11.ConnectionClosed):
                    break
                else:
                    events.append(event)

    assert [type(event).__name__ for event in events] == ["Response", "EndOfMessage"]
    assert events[0].status_code == status


def _recording_app(seen: list[str]) -> Callable:
    """Return an app that records every path it is handed.

    Whether the app ran at all is the thing being asserted, and a real app keeping
    a list says it without a mock standing in for one.
    """

    async def _app(scope: Scope, _receive: Callable, send: Callable) -> None:
        seen.append(cast("HTTPScope", scope)["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return _app


# Targets that no ASGI scope can carry a path for, driven at both stream types
NO_ASGI_PATH = [
    b"/caf\xc3\xa9",  # raw UTF-8, as HTTP/2 hands it over
    b"/bad\xff",  # raw, and not UTF-8 at all
    b"/a b",  # a space
    b"/x\x7f",  # DEL
    b"/bad%ff",  # a valid escape for a byte that is not UTF-8
    b"/x%zz",  # not a hex escape at all
]


@pytest.mark.anyio
@pytest.mark.parametrize("target", NO_ASGI_PATH)
async def test_http2_rejects_a_target_with_no_asgi_path(target: bytes) -> None:
    """400, and the app is never reached, for a target with no ASGI path.

    HTTP/2 carries :path as opaque octets, so these arrive at the server exactly as
    written - which is what makes them worth driving over a real connection rather
    than handing to HTTPStream directly.
    """
    seen: list[str] = []
    async with serve_in_memory(_recording_app(seen), alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())
        stream_id = client.get_next_available_stream_id()
        client.send_headers(
            stream_id,
            [
                (b":method", b"GET"),
                (b":path", target),
                (b":authority", b"anycorn"),
                (b":scheme", b"https"),
            ],
            end_stream=True,
        )
        await client_stream.send_all(client.data_to_send())

        status = None
        with anyio.fail_after(5):
            while status is None:
                data = await client_stream.receive_some(4096)
                if data == b"":
                    break
                for event in client.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived):
                        status = dict(event.headers)[b":status"]

    assert status == b"400"
    assert seen == [], "the app was handed a request it should never have seen"


@pytest.mark.anyio
@pytest.mark.parametrize("target", NO_ASGI_PATH)
async def test_http2_websocket_rejects_a_target_with_no_asgi_path(target: bytes) -> None:
    """The same rule on the websocket side, where WSStream builds the scope.

    The same targets as the HTTP test: WSStream has its own copy of the check and
    its own scope to build, so agreeing with HTTPStream is not something the two
    get for free.
    """
    seen: list[str] = []
    async with serve_in_memory(_recording_app(seen), alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())
        stream_id = client.get_next_available_stream_id()
        client.send_headers(
            stream_id,
            [
                (b":method", b"CONNECT"),
                (b":path", target),
                (b":authority", b"anycorn"),
                (b":scheme", b"https"),
                (b"sec-websocket-version", b"13"),
            ],
        )
        await client_stream.send_all(client.data_to_send())

        status = None
        with anyio.fail_after(5):
            while status is None:
                data = await client_stream.receive_some(4096)
                if data == b"":
                    break
                for event in client.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived):
                        status = dict(event.headers)[b":status"]

    assert status == b"400"
    assert seen == []


@pytest.mark.anyio
async def test_http2_rejected_request_may_still_send_a_body() -> None:
    """A peer that sends a body before hearing the rejection must not break the rest.

    The response goes out as soon as the headers are read, but the peer is already
    sending and cannot know that yet. The stream is gone by the time its DATA
    arrives, and indexing self.streams for it raised KeyError out of the connection's
    event loop - killing the connection over a request that had already been answered.
    """
    seen: list[str] = []
    async with serve_in_memory(_recording_app(seen), alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())
        stream_id = client.get_next_available_stream_id()
        client.send_headers(
            stream_id,
            [
                (b":method", b"POST"),
                (b":path", b"/bad\xff"),
                (b":authority", b"anycorn"),
                (b":scheme", b"https"),
            ],
        )
        await client_stream.send_all(client.data_to_send())
        await anyio.sleep(0.1)  # let the server answer before the body is sent
        client.send_data(stream_id, b"body-after-rejection", end_stream=True)
        await client_stream.send_all(client.data_to_send())

        status = None
        with anyio.fail_after(5):
            while status is None:
                data = await client_stream.receive_some(4096)
                if data == b"":
                    break
                for event in client.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived):
                        status = dict(event.headers)[b":status"]

    assert status == b"400"
    assert seen == []
