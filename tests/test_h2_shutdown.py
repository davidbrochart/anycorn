"""Tests that an HTTP/2 connection is wound down when the worker is shutting down."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import h2.connection
import h2.events
import h2.settings
import pytest

from anycorn.worker_context import WorkerContext

from .helpers import serve_in_memory

if TYPE_CHECKING:
    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope

OK = 200


class _Recorder:
    """An ASGI app that records the paths it was actually asked to serve.

    A refused request must never reach it, which is a stronger statement than the
    client having been sent a reset: it says no application work was started.
    """

    def __init__(self, release: anyio.Event) -> None:
        self.paths: list[str] = []
        self._release = release

    async def __call__(
        self, scope: Scope, _receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        assert scope["type"] == "http"
        self.paths.append(scope["path"])
        await self._release.wait()
        await send(
            {
                "type": "http.response.start",
                "status": OK,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _send_request(client: h2.connection.H2Connection, path: str) -> int:
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
    return stream_id


async def _pump(
    client: h2.connection.H2Connection,
    client_stream: Any,  # noqa: ANN401
    until: type[h2.events.Event],
    stream_id: int | None = None,
) -> tuple[h2.events.Event, dict]:
    """Read until *until* arrives, returning it and every setting seen along the way.

    Each read is drained in full before returning, because the frames that accompany
    the awaited event arrive with it - a reset is written in the same flush as the
    settings update that goes with it, and stopping at the reset would miss it.
    """
    settings: dict = {}
    found: h2.events.Event | None = None
    while found is None:
        data = await client_stream.receive_some(2**16)
        assert data != b"", "connection closed before the expected event arrived"
        for event in client.receive_data(data):
            if isinstance(event, h2.events.RemoteSettingsChanged):
                settings.update(event.changed_settings)
            if (
                found is None
                and isinstance(event, until)
                and (stream_id is None or getattr(event, "stream_id", None) == stream_id)
            ):
                found = event
        await client_stream.send_all(client.data_to_send())
    return found, settings


async def _connected(client_stream: Any) -> h2.connection.H2Connection:  # noqa: ANN401
    client = h2.connection.H2Connection()
    client.initiate_connection()
    await client_stream.send_all(client.data_to_send())
    return client


@pytest.mark.anyio
async def test_http2_refuses_a_new_request_once_terminated() -> None:
    """A request arriving after the worker starts shutting down is reset, not served.

    Both requests run against the same connection, so the first is the control: it
    shows a request of the very same shape reaching the application whilst the worker
    runs, leaving termination as the only thing that differs when the second is
    refused. It is also held in flight deliberately - an idle HTTP/2 connection is
    closed outright when the worker is marked terminated, so a connection still
    carrying a request is what puts this check in reach at all.
    """
    release = anyio.Event()
    app = _Recorder(release)
    context = WorkerContext(None)

    async with serve_in_memory(app, alpn_protocol="h2", context=context) as client_stream:
        client = await _connected(client_stream)

        served = _send_request(client, "/served")
        await client_stream.send_all(client.data_to_send())
        await anyio.wait_all_tasks_blocked()
        # The control: accepted, and being worked on, whilst the worker is running
        assert app.paths == ["/served"]

        await context.terminated.set()

        refused = _send_request(client, "/refused")
        await client_stream.send_all(client.data_to_send())
        with anyio.fail_after(5):
            _, settings = await _pump(client, client_stream, h2.events.StreamReset, refused)

        # The request already in hand is still owed a response
        release.set()
        with anyio.fail_after(5):
            await _pump(client, client_stream, h2.events.StreamEnded, served)

    # Reset, and told to open nothing further
    max_concurrent = settings[h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS]
    assert max_concurrent.new_value == 0
    # The refused request never reached the application
    assert app.paths == ["/served"]


@pytest.mark.anyio
async def test_http2_closes_the_connection_once_the_last_stream_ends() -> None:
    """The response in flight is still delivered, then the connection is closed.

    GOAWAY only goes out once the connection falls idle, so a worker that is shutting
    down does not cut off the request it is already serving.
    """
    release = anyio.Event()
    app = _Recorder(release)
    context = WorkerContext(None)

    async with serve_in_memory(app, alpn_protocol="h2", context=context) as client_stream:
        client = await _connected(client_stream)

        stream_id = _send_request(client, "/in-flight")
        await client_stream.send_all(client.data_to_send())
        await anyio.wait_all_tasks_blocked()

        # Terminated whilst the app is still working: the response is owed
        await context.terminated.set()
        release.set()

        with anyio.fail_after(5):
            # Filtered on the stream, so arriving at all is the response being delivered
            await _pump(client, client_stream, h2.events.StreamEnded, stream_id)
            terminated, _ = await _pump(client, client_stream, h2.events.ConnectionTerminated)

    assert isinstance(terminated, h2.events.ConnectionTerminated)
    assert app.paths == ["/in-flight"]
