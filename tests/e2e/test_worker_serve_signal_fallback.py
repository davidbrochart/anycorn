"""Integration test for worker_serve's SIGINT/SIGTERM fallback.

hypercorn's asyncio backend installs a fallback SIGINT/SIGTERM/SIGBREAK handler
inside worker_serve whenever the caller doesn't supply a shutdown_trigger, so
that anycorn.serve(app, config) and single-worker (--workers 0) invocations
still shut down gracefully - respecting graceful_timeout, same as every other
shutdown source - on Ctrl-C or SIGTERM, without the caller doing anything.
Without it, SIGTERM has no handling at all (Python's default disposition just
kills the process), and SIGINT falls back to a raw KeyboardInterrupt instead
of a graceful shutdown.

anycorn ported this for its asyncio backend only, matching hypercorn's own
asymmetry: hypercorn's trio backend has no equivalent fallback at all, relying
solely on trio's built-in SIGINT-to-cancellation behaviour with no SIGTERM
handling whatsoever. This test drives both backends through a real subprocess
and asserts that asymmetry directly, rather than only checking the asyncio side.

This only engages for --workers 0: with workers >= 1 (the default), the
multiprocess supervisor in run.py already handles SIGINT/SIGTERM itself and
passes each worker a real shutdown_trigger derived from a shared
multiprocessing.Event, so worker_serve never sees shutdown_trigger=None there.

test_workers_0_sigterm_shutdown covers POSIX; it's skipped on Windows because
Popen.send_signal(SIGTERM) maps straight to TerminateProcess there, bypassing
any handler. test_workers_0_ctrl_break_shutdown is the Windows-only analogue,
using CTRL_BREAK_EVENT/SIGBREAK - the one console signal Windows lets you
target at just the child process (see its docstring for why SIGINT itself
can't be tested this way, and why it spawns the child differently).
"""

from __future__ import annotations

import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import httpx2
import pytest

from tests.e2e._subprocess import REPO_ROOT, anycorn_subprocess

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

APP_PATH = "tests/assets/pid_app.py:app"


async def _try_get(base_url: str) -> bool:
    try:
        async with httpx2.AsyncClient(base_url=base_url) as client:
            await client.get("/")
    except httpx2.TransportError:
        return False
    else:
        return True


async def _wait_until_ready(base_url: str) -> None:
    with anyio.fail_after(10):
        while True:
            if await _try_get(base_url):
                return
            await anyio.sleep(0.1)


@pytest.mark.anyio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Popen.send_signal(SIGTERM) maps directly to TerminateProcess() on "
    "Windows - it isn't real signal delivery, so no handler can intercept it.",
)
async def test_workers_0_sigterm_shutdown(anyio_backend_name: str, free_tcp_port: int) -> None:
    """SIGTERM to a --workers 0 process is only handled gracefully on asyncio."""
    args = [APP_PATH, "--bind", f"127.0.0.1:{free_tcp_port}", "--workers", "0"]
    async with anycorn_subprocess(args, anyio_backend_name=anyio_backend_name) as process:
        await _wait_until_ready(f"http://127.0.0.1:{free_tcp_port}")

        process.send_signal(signal.SIGTERM)

        with anyio.fail_after(10):
            returncode = await process.wait()

        if anyio_backend_name == "asyncio":
            assert returncode == 0
        else:
            # hypercorn's own trio backend has no fallback either - SIGTERM's
            # default disposition just kills the process outright
            assert returncode != 0


def _wait_bounded(process: subprocess.Popen[bytes], timeout: float) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


@asynccontextmanager
async def _new_process_group_subprocess(
    args: Sequence[str], *, anyio_backend_name: str
) -> AsyncIterator[subprocess.Popen[bytes]]:
    """Spawn ``python -m anycorn <args>`` in its own console process group.

    Unlike anycorn_subprocess, this deliberately avoids anyio.open_process: on
    Windows its asyncio backend drives subprocess creation through
    ProactorEventLoop's IOCP-based subprocess_exec(), which conflicts with
    creationflags=CREATE_NEW_PROCESS_GROUP - the child fails or hangs on
    startup instead of running normally. A plain, blocking subprocess.Popen()
    run in a worker thread sidesteps ProactorEventLoop entirely, avoiding that
    conflict; the process itself doesn't care which mechanism spawned it.
    """
    process = await anyio.to_thread.run_sync(
        lambda: subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "anycorn",
                *args,
                "--worker-class",
                anyio_backend_name,
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        await anyio.to_thread.run_sync(_wait_bounded, process, 5)


@pytest.mark.anyio
@pytest.mark.skipif(
    sys.platform != "win32",
    reason="CTRL_BREAK_EVENT is a Windows-only console signal.",
)
async def test_workers_0_ctrl_break_shutdown(anyio_backend_name: str, free_tcp_port: int) -> None:
    """CTRL_BREAK_EVENT (SIGBREAK) is the Windows analogue of the SIGINT/SIGTERM test above.

    Real CTRL_C_EVENT can't be aimed at a single child process on Windows:
    GenerateConsoleCtrlEvent only accepts process-group 0 for it, which means
    "every process sharing this console" - including the test runner itself.
    CTRL_BREAK_EVENT is the one console signal that *can* be targeted at just
    the child, via CREATE_NEW_PROCESS_GROUP, and Python maps it to SIGBREAK -
    which is in anycorn's asyncio fallback list alongside SIGINT and SIGTERM.
    """
    args = [APP_PATH, "--bind", f"127.0.0.1:{free_tcp_port}", "--workers", "0"]
    async with _new_process_group_subprocess(
        args, anyio_backend_name=anyio_backend_name
    ) as process:
        await _wait_until_ready(f"http://127.0.0.1:{free_tcp_port}")

        process.send_signal(signal.CTRL_BREAK_EVENT)  # ty:ignore[unresolved-attribute]

        returncode = await anyio.to_thread.run_sync(_wait_bounded, process, 10)

        if anyio_backend_name == "asyncio":
            assert returncode == 0
        else:
            # trio's Ctrl-C protection is specific to SIGINT; it has no SIGBREAK
            # handling, so this falls back to Windows' default disposition
            assert returncode != 0
