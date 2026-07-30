"""A real Starlette app, over a real socket, must see the client hang up.

Companion to tests/test_starlette_disconnect.py, which drives spawn_app in
memory. This one runs a real anycorn server on a real TCP port with a real
Starlette app, and hangs up a real client socket while a handler is polling
Request.is_disconnected() - the exact shape of a long-running handler noticing
its client has gone. Without the leading-checkpoint fix in task_group the
queued http.disconnect is never handed over and the handler never notices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import anycorn
from anycorn.config import Config

if TYPE_CHECKING:
    from starlette.requests import Request

# The handler gives up polling after this long so a regression fails the test
# (via the detected-event timeout below) rather than hanging it.
_DETECT_TIMEOUT = 5.0


@pytest.mark.anyio
async def test_starlette_sees_a_real_socket_disconnect(free_tcp_port: int) -> None:
    """A polling handler must detect a disconnect delivered over a real socket."""
    started = anyio.Event()
    detected = anyio.Event()

    async def endpoint(request: Request) -> PlainTextResponse:
        started.set()
        with anyio.move_on_after(_DETECT_TIMEOUT):
            # Polling is_disconnected() is the mechanism under test - exactly what a
            # long-running Starlette handler does - so an Event wait can't replace it.
            while not await request.is_disconnected():  # noqa: ASYNC110
                await anyio.sleep(0.01)
            detected.set()
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])

    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()
        await tg.start(
            lambda *, task_status: anycorn.serve(
                app, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )

        stream = await anyio.connect_tcp("127.0.0.1", free_tcp_port)
        try:
            await stream.send(b"GET / HTTP/1.1\r\nhost: anycorn\r\n\r\n")
            # Wait until the handler is actually running and polling before hanging
            # up, so this is a mid-request disconnect, not a race with startup.
            with anyio.fail_after(10):
                await started.wait()
        finally:
            await stream.aclose()  # the client goes away

        with anyio.fail_after(10):
            await detected.wait()
        shutdown.set()

    assert detected.is_set()
