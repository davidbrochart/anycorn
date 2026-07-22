"""Integration test for SIGHUP-triggered worker reloads.

This spawns a real ``python -m anycorn`` subprocess and sends it a genuine
SIGHUP. Unlike the mocked unit test in ``tests/test_run.py``, SIGHUP handling
lives entirely in run.py's synchronous multiprocess orchestration, so nothing
that drives anycorn in-process (e.g. ``anycorn.serve()``) ever exercises it -
a real child process is the only way to see the reload actually happen.
"""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING

import anyio
import httpx2
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from anyio.abc import Process

APP_PATH = "tests/assets/pid_app.py:app"


async def _fetch_pid(base_url: str) -> str | None:
    """Return the worker's PID from a brand-new connection, or None if not ready.

    A fresh client per call, rather than one reused across the whole test, matters
    here: a kept-alive connection would stay pinned to whichever worker accepted it,
    so it could never observe a new worker taking over the listening socket after
    the reload. Opening a new connection each time is what actually proves it.
    """
    try:
        async with httpx2.AsyncClient(base_url=base_url) as client:
            response = await client.get("/")
    except httpx2.TransportError:
        return None
    else:
        return response.text.strip()


async def _wait_for_pid(base_url: str, *, differs_from: str | None) -> str:
    with anyio.fail_after(10):
        while True:
            pid = await _fetch_pid(base_url)
            if pid is not None and pid != differs_from:
                return pid
            await anyio.sleep(0.1)


@pytest.mark.anyio
async def test_sighup_reloads_the_worker(
    anyio_backend_name: str,
    free_tcp_port: int,
    anycorn_subprocess: Callable[[Sequence[str]], AbstractAsyncContextManager[Process]],
) -> None:
    """A real SIGHUP to the anycorn parent process must gracefully restart its worker.

    The spawned worker's own event loop backend is pinned to match this test's
    anyio_backend, so the trio-parametrised run genuinely exercises a trio worker
    end-to-end instead of always falling back to anycorn's asyncio default
    regardless of which backend the test itself is using.
    """
    args = [
        APP_PATH,
        "--bind",
        f"127.0.0.1:{free_tcp_port}",
        "--workers",
        "1",
        "--worker-class",
        anyio_backend_name,
    ]
    async with anycorn_subprocess(args) as process:
        base_url = f"http://127.0.0.1:{free_tcp_port}"
        pid_before = await _wait_for_pid(base_url, differs_from=None)
        assert pid_before.isdigit()

        process.send_signal(signal.SIGHUP)

        pid_after = await _wait_for_pid(base_url, differs_from=pid_before)
        assert pid_after.isdigit()
        assert pid_after != pid_before
