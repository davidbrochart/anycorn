"""Integration test for DispatcherMiddleware not mutating the caller's scope.

DispatcherMiddleware used to mutate scope["path"] in place - stripping off the
mount prefix - rather than copying the scope and extending root_path instead.
Because HTTPStream keeps and reuses the very same scope dict it handed to the
app, for the access log call that runs after the app has finished, that
mutation was externally observable: anycorn's own access log would record the
mount-stripped path instead of the path the client actually requested.

This drives a real, running anycorn server (in-process, not a subprocess:
nothing here needs real OS-level process semantics) with a real HTTP client
through a DispatcherMiddleware-mounted app, and inspects the actual access
log record anycorn emitted for the request - not the scope passed to the
mounted app directly, since that alone can't tell an in-place mutation of the
caller's dict apart from building and passing a correct new one.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
import httpx2
import pytest

import anycorn
from anycorn.config import Config
from anycorn.middleware.dispatcher import DispatcherMiddleware


async def mounted_app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    body = f"path={scope['path']} root_path={scope['root_path']}".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.anyio
async def test_access_log_records_the_full_request_path_through_dispatcher(
    free_tcp_port: int,
) -> None:
    """The dispatcher must not corrupt the scope object the access log reads afterward."""
    handler = _RecordingHandler()
    access_logger = logging.getLogger("test_dispatcher_access_log")
    access_logger.setLevel(logging.INFO)
    access_logger.handlers = [handler]
    access_logger.propagate = False

    app = DispatcherMiddleware({"/api": mounted_app})

    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = access_logger
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()
        await tg.start(
            lambda *, task_status: anycorn.serve(
                app, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{free_tcp_port}") as client:
            with anyio.fail_after(10):
                response = await client.get("/api/hello")
        shutdown.set()

    assert response.status_code == 200  # noqa: PLR2004
    # The mounted app itself sees the full path unchanged, with root_path
    # extended - not a truncated path with root_path left alone
    assert response.text == "path=/api/hello root_path=/api"

    assert len(handler.records) == 1
    logged_args = handler.records[0].args
    assert isinstance(logged_args, dict)
    assert logged_args["U"] == "/api/hello"
