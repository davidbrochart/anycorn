"""A real Starlette app must see a client disconnect through anycorn's receive.

Starlette's Request.is_disconnected() polls receive() inside an already-cancelled
scope, so a receive() that checkpoints before returning a buffered http.disconnect
never hands it over and the app never learns the client has gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.task_group import TaskGroup
from anycorn.typing import ConnectionState

if TYPE_CHECKING:
    from starlette.requests import Request

    from anycorn.typing import ASGIFramework, HTTPScope

_MAX_POLLS = 50


def _http_scope() -> HTTPScope:
    return {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"spec_version": "2.1", "version": "3.0"},
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"anycorn")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
        "extensions": {},
        "state": ConnectionState({}),
    }


@pytest.mark.anyio
async def test_starlette_app_detects_a_client_disconnect() -> None:
    """A real Starlette handler polling is_disconnected() must see a queued disconnect."""
    detected: dict[str, bool] = {}

    async def endpoint(request: Request) -> PlainTextResponse:
        # As a long-running handler does: check periodically whether the client is
        # still there. Without the fix this stays False forever and loops out.
        for _ in range(_MAX_POLLS):
            if await request.is_disconnected():
                detected["disconnected"] = True
                break
            await anyio.sleep(0.001)
        else:
            detected["disconnected"] = False
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])

    responses: list[Any] = []

    async def send(message: Any) -> None:  # noqa: ANN401
        if message is not None:
            responses.append(message)

    with anyio.fail_after(5):
        async with TaskGroup() as task_group:
            put = await task_group.spawn_app(
                ASGIWrapper(cast("ASGIFramework", app)), Config(), _http_scope(), send
            )
            # The client goes away: a disconnect is queued for the running handler.
            await put({"type": "http.disconnect"})

    assert detected["disconnected"] is True
