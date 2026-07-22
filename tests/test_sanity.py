"""Sanity tests for Anycorn.

These drive a real `TCPServer` over an in-memory socket, speaking the wire protocol
from the client side, so they cover the whole stack from bytes to ASGI and back.
"""

from __future__ import annotations

import h2.config
import h2.connection
import h2.events
import h11
import pytest
import wsproto
import wsproto.connection
import wsproto.events

from .helpers import SANITY_BODY, sanity_framework, serve_in_memory


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
