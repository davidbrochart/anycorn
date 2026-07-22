"""Integration test for the --daemon / Config.daemon option.

Config.daemon controls process.daemon on each spawned worker, which run.py
used to hardcode to True. The concrete, OS-level difference that makes: a
daemonic process is not allowed to spawn its own child processes - Python's
multiprocessing raises AssertionError from Process.start() when the calling
process is itself daemonic. This spawns two real `python -m anycorn`
subprocesses - one with the (default) daemon=True and one configured with
daemon=False via a config file, since there is no CLI flag to turn it off -
and checks that only the worker with daemon=False can create a child of
its own.
"""

from __future__ import annotations

import anyio
import httpx2
import pytest

from tests.e2e._subprocess import anycorn_subprocess

APP_PATH = "tests/assets/daemon_app.py:app"
DAEMON_FALSE_CONFIG = "file:tests/assets/config_daemon_false.py"


async def _try_fetch_body(base_url: str) -> str | None:
    try:
        async with httpx2.AsyncClient(base_url=base_url) as client:
            response = await client.get("/")
    except httpx2.TransportError:
        return None
    else:
        return response.text


async def _fetch_body(base_url: str) -> str:
    with anyio.fail_after(10):
        while True:
            body = await _try_fetch_body(base_url)
            if body is not None:
                return body
            await anyio.sleep(0.1)


@pytest.mark.anyio
async def test_daemon_worker_cannot_spawn_children(
    anyio_backend_name: str, free_tcp_port: int
) -> None:
    """Config.daemon defaults to True, matching what run.py used to hardcode."""
    args = [APP_PATH, "--bind", f"127.0.0.1:{free_tcp_port}", "--workers", "1"]
    async with anycorn_subprocess(args, anyio_backend_name=anyio_backend_name):
        body = await _fetch_body(f"http://127.0.0.1:{free_tcp_port}")

    assert body == "daemonic processes are not allowed to have children"


@pytest.mark.anyio
async def test_non_daemon_worker_can_spawn_children(
    anyio_backend_name: str, free_tcp_port: int
) -> None:
    """With daemon=False configured, the worker is free to create its own children."""
    args = [
        APP_PATH,
        "--bind",
        f"127.0.0.1:{free_tcp_port}",
        "--workers",
        "1",
        "--config",
        DAEMON_FALSE_CONFIG,
    ]
    async with anycorn_subprocess(args, anyio_backend_name=anyio_backend_name):
        body = await _fetch_body(f"http://127.0.0.1:{free_tcp_port}")

    assert body == "ok"
