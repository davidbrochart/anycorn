"""Tests that ASGI state is per request rather than per connection.

ASGI has the server pass "a shallow copy of the namespace ... into each subsequent
request/response call", so two requests sharing a connection must not share what they
put there. Multiplexed protocols make that easy to get wrong: h2 and h3 carry many
requests over one connection, and the connection is the obvious place to copy once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import h2.connection
import h2.events
import h11
import pytest

from .helpers import serve_in_memory

if TYPE_CHECKING:
    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, HTTPScope

REQUESTS = 2


class _StateRecorder:
    """Records the state it is handed, then writes to it."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        # The namespaces themselves, not their ids: a freed dict's address can be
        # handed straight back to the next one, so ids only tell objects apart
        # whilst both are alive
        self.states: list[dict] = []

    async def __call__(
        self, scope: HTTPScope, _receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        state = scope["state"]
        self.seen.append(dict(state))
        self.states.append(state)
        state["secret"] = scope["path"]

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})


@pytest.mark.anyio
async def test_http1_requests_do_not_share_state() -> None:
    """Two requests on one kept-alive connection each get their own namespace."""
    app = _StateRecorder()

    async with serve_in_memory(app) as client_stream:
        client = h11.Connection(h11.CLIENT)
        for index in range(REQUESTS):
            if index:
                client.start_next_cycle()
            request = h11.Request(method="GET", target=f"/{index}", headers=[(b"host", b"anycorn")])
            await client_stream.send_all(client.send(request))
            await client_stream.send_all(client.send(h11.EndOfMessage()))
            while True:
                event = client.next_event()
                if event is h11.NEED_DATA:
                    client.receive_data(await client_stream.receive_some(2**16))
                elif isinstance(event, h11.EndOfMessage):
                    break

    assert app.seen == [{}, {}]
    first, second = app.states
    assert first is not second


@pytest.mark.anyio
async def test_http2_requests_do_not_share_state() -> None:
    """Two requests multiplexed on one h2 connection each get their own namespace."""
    app = _StateRecorder()

    async with serve_in_memory(app, alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())

        for index in range(REQUESTS):
            stream_id = client.get_next_available_stream_id()
            client.send_headers(
                stream_id,
                [
                    (b":method", b"GET"),
                    (b":path", f"/{index}".encode()),
                    (b":authority", b"anycorn"),
                    (b":scheme", b"https"),
                ],
                end_stream=True,
            )
        await client_stream.send_all(client.data_to_send())

        ended: set[int] = set()
        while len(ended) < REQUESTS:
            data = await client_stream.receive_some(2**16)
            assert data != b"", "connection closed before both responses arrived"
            for event in client.receive_data(data):
                if isinstance(event, h2.events.DataReceived):
                    client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    ended.add(event.stream_id)
            await client_stream.send_all(client.data_to_send())

    # Neither request saw what the other stored, and neither shared its namespace
    assert app.seen == [{}, {}]
    first, second = app.states
    assert first is not second
