"""End-to-end tests using httpx2."""

from __future__ import annotations

from typing import Any

import anyio
import httpx2
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


@pytest.mark.anyio
async def test_keep_alive_max_requests_regression(free_tcp_port: int) -> None:
    config = Config()
    # A fixed port is one an unrelated listener, or a second job on the same machine,
    # can already be holding
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = "-"  # Log to stdout/err
    config.errorlog = "-"
    config.keep_alive_max_requests = 2

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()

        # Started rather than spawned, so the socket is listening before the first
        # request rather than merely likely to be
        await tg.start(
            lambda *, task_status: anycorn.serve(
                app, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )

        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{free_tcp_port}") as client:
            # Make sure that we properly clean up connections when `keep_alive_max_requests`
            # is hit such that the client stays good over multiple hangups.
            for _ in range(10):
                result = await client.post("/test", json={"key": "value"})
                result.raise_for_status()

        shutdown.set()


@pytest.mark.anyio
async def test_server_cancelled_mid_request(free_tcp_port: int) -> None:
    started = anyio.Event()
    request_finished = anyio.Event()
    request_error: httpx2.TransportError | None = None
    shutdown = anyio.Event()

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
        started.set()
        await anyio.sleep_forever()

    async def make_request(client: httpx2.AsyncClient) -> None:
        nonlocal request_error

        try:
            await client.get("/")
        except httpx2.TransportError as error:
            request_error = error
        finally:
            request_finished.set()

    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = "-"
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        await tg.start(
            lambda *, task_status: anycorn.serve(
                app,
                config,
                shutdown_trigger=shutdown.wait,
                task_status=task_status,
            )
        )

        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{free_tcp_port}") as client:
            tg.start_soon(make_request, client)
            with anyio.fail_after(10):
                await started.wait()

            shutdown.set()

            with anyio.fail_after(10):
                await request_finished.wait()

    assert request_error is not None
