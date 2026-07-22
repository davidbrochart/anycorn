"""Tests for HTTP keep-alive connection handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import h11
import pytest

from anycorn.config import Config
from anycorn.worker_context import WorkerContext

from .helpers import serve_in_memory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anycorn.typing import ASGIReceiveEvent, ASGISendEvent, Scope

    from .helpers import MemoryClientStream


OK = 200
PIPELINED_REQUESTS = 2
REQUEST = h11.Request(method="GET", target="/", headers=[(b"host", b"anycorn")])


class ManualIdleTimeout(WorkerContext):
    """A worker whose keep-alive timeout fires when the test says so, not on a clock.

    `move_on_after()` hands back a scope with no deadline, so a connection stays idle
    indefinitely until `expire()` cancels it. Waiting out a real timeout instead makes
    the test a race between the deadline and however long a loaded machine takes to
    get the request read - which it can lose in either direction.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self._scopes: list[anyio.CancelScope] = []

    def move_on_after(self, delay: float | None) -> anyio.CancelScope:  # noqa: ARG002
        scope = anyio.CancelScope()
        self._scopes.append(scope)
        return scope

    def expire(self) -> None:
        """Fire every keep-alive timeout currently being waited on."""
        for scope in self._scopes:
            scope.cancel()


def gated_framework(release: anyio.Event) -> Callable:
    """Return an app that will not answer until *release* is set."""

    async def _app(
        _scope: Scope,
        receive: Callable[[], Awaitable[ASGIReceiveEvent]],
        send: Callable[[ASGISendEvent], Awaitable[None]],
    ) -> None:
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                break
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
            elif event["type"] == "http.request" and not event.get("more_body", False):
                await release.wait()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                break

    return _app


def _config() -> Config:
    config = Config()
    # Never reached: ManualIdleTimeout ignores the delay and waits to be told
    config.keep_alive_timeout = 60.0
    return config


async def _read_response(client: h11.Connection, client_stream: MemoryClientStream) -> h11.Response:
    """Read one complete response, returning its head."""
    response = None
    while True:
        event = client.next_event()
        if event is h11.NEED_DATA:
            client.receive_data(await client_stream.receive_some(1024))
        elif isinstance(event, h11.Response):
            response = event
        elif isinstance(event, h11.EndOfMessage):
            assert response is not None
            return response


@pytest.mark.anyio
async def test_http1_keep_alive_pre_request() -> None:
    """A connection idle before its request completes is closed."""
    context = ManualIdleTimeout()
    released = anyio.Event()
    released.set()
    async with serve_in_memory(
        gated_framework(released), _config(), context=context
    ) as client_stream:
        await client_stream.send_all(b"GET")
        await anyio.wait_all_tasks_blocked()

        context.expire()
        await anyio.wait_all_tasks_blocked()

        # Only way to confirm closure is to invoke an error
        with pytest.raises(anyio.BrokenResourceError):
            await client_stream.send_all(b"a")


@pytest.mark.anyio
async def test_http1_keep_alive_during() -> None:
    """The idle timeout must not fire whilst the app is still working."""
    context = ManualIdleTimeout()
    release = anyio.Event()
    async with serve_in_memory(
        gated_framework(release), _config(), context=context
    ) as client_stream:
        client = h11.Connection(h11.CLIENT)
        await client_stream.send_all(client.send(REQUEST))
        await client_stream.send_all(client.send(h11.EndOfMessage()))
        await anyio.wait_all_tasks_blocked()

        # Fire the timeout whilst the app is mid-request: it has been stopped for the
        # duration, so the connection must survive it
        context.expire()
        await anyio.wait_all_tasks_blocked()
        release.set()

        assert (await _read_response(client, client_stream)).status_code == OK


@pytest.mark.anyio
async def test_http1_keep_alive() -> None:
    """A second request on the same connection is served."""
    release = anyio.Event()
    release.set()
    async with serve_in_memory(gated_framework(release), _config()) as client_stream:
        client = h11.Connection(h11.CLIENT)
        await client_stream.send_all(client.send(REQUEST))
        await client_stream.send_all(client.send(h11.EndOfMessage()))
        assert (await _read_response(client, client_stream)).status_code == OK

        client.start_next_cycle()
        await client_stream.send_all(client.send(REQUEST))
        await client_stream.send_all(client.send(h11.EndOfMessage()))
        assert (await _read_response(client, client_stream)).status_code == OK


@pytest.mark.anyio
async def test_http1_keep_alive_pipelining() -> None:
    """Two requests sent back to back are both served."""
    release = anyio.Event()
    release.set()
    async with serve_in_memory(gated_framework(release), _config()) as client_stream:
        await client_stream.send_all(
            b"GET / HTTP/1.1\r\nHost: anycorn\r\n\r\nGET / HTTP/1.1\r\nHost: anycorn\r\n\r\n"
        )
        # Read the responses off the wire rather than through h11, which drives one
        # request/response cycle at a time and so cannot parse a pipelined pair
        received = b""
        with anyio.fail_after(5):
            while received.count(b"HTTP/1.1 200") < PIPELINED_REQUESTS:
                chunk = await client_stream.receive_some(1024)
                assert chunk != b"", "connection closed before both responses arrived"
                received += chunk
