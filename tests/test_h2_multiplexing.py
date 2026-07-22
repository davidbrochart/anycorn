"""Tests that HTTP/2 carries concurrent requests on a single connection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import h2.connection
import h2.events
import pytest

from .helpers import serve_in_memory

if TYPE_CHECKING:
    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope

CONCURRENT_REQUESTS = 3
OK = 200


class _Rendezvous:
    """An ASGI app that answers nothing until every request has arrived.

    A response arriving at all is then evidence the server is carrying them at once.
    Were it taking one at a time, the first would sit waiting on siblings that cannot
    be read yet, and the caller would time out rather than pass.
    """

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.arrived: list[str] = []
        self.states: list[int] = []
        self.released = anyio.Event()

    async def __call__(
        self, scope: Scope, _receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        assert scope["type"] == "http"
        assert scope["http_version"] == "2"
        self.arrived.append(scope["path"])
        # Identity, not contents: ASGI copies this per connection, so one namespace
        # across every request means one connection carried them
        self.states.append(id(scope["state"]))
        if len(self.arrived) >= self.expected:
            self.released.set()

        await self.released.wait()
        body = scope["path"].encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"%d" % len(body))],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_responses(
    client: h2.connection.H2Connection,
    client_stream: Any,  # noqa: ANN401
    expected: int,
) -> dict[int, bytes]:
    """Collect one complete response per stream, until *expected* have ended."""
    bodies: dict[int, bytearray] = {}
    ended: set[int] = set()
    while len(ended) < expected:
        data = await client_stream.receive_some(2**16)
        assert data != b"", "connection closed before every response arrived"
        for event in client.receive_data(data):
            if isinstance(event, h2.events.DataReceived):
                bodies.setdefault(event.stream_id, bytearray()).extend(event.data)
                client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.ResponseReceived):
                assert dict(event.headers)[b":status"] == b"%d" % OK
            elif isinstance(event, h2.events.StreamEnded):
                ended.add(event.stream_id)
        await client_stream.send_all(client.data_to_send())

    return {stream_id: bytes(body) for stream_id, body in bodies.items()}


@pytest.mark.anyio
async def test_concurrent_requests_share_one_connection() -> None:
    """HTTP/2 multiplexes concurrent requests rather than taking them in turn."""
    app = _Rendezvous(CONCURRENT_REQUESTS)

    async with serve_in_memory(app, alpn_protocol="h2") as client_stream:
        client = h2.connection.H2Connection()
        client.initiate_connection()
        await client_stream.send_all(client.data_to_send())

        paths = {}
        for index in range(CONCURRENT_REQUESTS):
            path = f"/{index}"
            stream_id = client.get_next_available_stream_id()
            client.send_headers(
                stream_id,
                [
                    (b":method", b"GET"),
                    (b":path", path.encode()),
                    (b":authority", b"anycorn"),
                    (b":scheme", b"https"),
                ],
                end_stream=True,
            )
            paths[stream_id] = path
        # Every request goes out before any response is read, so the server has all
        # three in hand at once
        await client_stream.send_all(client.data_to_send())

        with anyio.fail_after(5):
            bodies = await _read_responses(client, client_stream, CONCURRENT_REQUESTS)

    # Each answered, and answered as itself rather than as a sibling
    assert bodies == {stream_id: path.encode() for stream_id, path in paths.items()}
    assert sorted(app.arrived) == sorted(paths.values())
    # One state namespace, so one connection served all three
    assert len(set(app.states)) == 1
