"""End-to-end tests using httpx."""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

import anycorn
from anycorn.config import Config


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/plain"],
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"Hello, world!",
        }
    )


async def isolate_state_app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/plain"]],
        }
    )
    # Send back whatever the previous request may have left in state, and then
    # mutate the state. Per ASGI spec each request should get its own state,
    # so a second request on the same (keep-alive) connection shouldn't see this.
    await send(
        {
            "type": "http.response.body",
            "body": scope["state"].get("key", b""),
            "more_body": False,
        }
    )
    scope["state"]["key"] = b"one"


@pytest.mark.anyio
async def test_handle_isolate_state() -> None:
    config = Config()
    config.bind = ["127.0.0.1:1235"]
    config.accesslog = "-"
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()

        async def serve() -> None:
            await anycorn.serve(isolate_state_app, config, shutdown_trigger=shutdown.wait)

        tg.start_soon(serve)

        await anyio.wait_all_tasks_blocked()

        # A single client reuses one keep-alive connection for both requests.
        async with httpx.AsyncClient() as client:
            first = await client.get("http://127.0.0.1:1235/")
            second = await client.get("http://127.0.0.1:1235/")

        assert first.content == b""
        assert second.content == b""

        shutdown.set()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])  # FIXME
async def test_keep_alive_max_requests_regression() -> None:
    config = Config()
    config.bind = ["127.0.0.1:1234"]
    config.accesslog = "-"  # Log to stdout/err
    config.errorlog = "-"
    config.keep_alive_max_requests = 2

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()

        async def serve() -> None:
            await anycorn.serve(app, config, shutdown_trigger=shutdown.wait)

        tg.start_soon(serve)

        await anyio.wait_all_tasks_blocked()

        client = httpx.AsyncClient()

        # Make sure that we properly clean up connections when `keep_alive_max_requests`
        # is hit such that the client stays good over multiple hangups.
        for _ in range(10):
            result = await client.post("http://127.0.0.1:1234/test", json={"key": "value"})
            result.raise_for_status()

        shutdown.set()
