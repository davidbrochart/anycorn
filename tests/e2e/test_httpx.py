from __future__ import annotations

import anyio
import httpx
import pytest

import anycorn
from anycorn.config import Config


async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
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
