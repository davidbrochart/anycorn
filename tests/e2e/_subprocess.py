"""Shared helper for anycorn subprocess-based integration tests."""

from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from anyio.abc import Process

REPO_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def anycorn_subprocess(
    args: Sequence[str], *, anyio_backend_name: str
) -> AsyncIterator[Process]:
    """Run ``python -m anycorn <args>`` as a real subprocess, cleaned up on exit.

    Used by tests that need to observe behaviour only run.py's real, synchronous
    multiprocess orchestration produces (signal handling, worker daemon status) -
    nothing driven in-process via anycorn.serve() exercises that code at all.

    Always appends ``--worker-class <anyio_backend_name>``, so the worker's own
    event loop backend matches the caller's - otherwise the worker would default
    to asyncio regardless of which backend the test itself is parametrised under,
    silently skipping half the coverage a trio-parametrised run is meant to give.
    anyio_backend_name is a required keyword rather than pulled from a fixture, so
    a caller that forgets it fails immediately instead of silently getting asyncio.
    """
    process = await anyio.open_process(
        [sys.executable, "-m", "anycorn", *args, "--worker-class", anyio_backend_name],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        # The process may already have exited on its own (e.g. a test that sends
        # it a shutdown signal itself) - terminate()/kill() on an already-reaped
        # process raises ProcessLookupError under the asyncio backend, so check
        # returncode before each, not just before the second attempt
        if process.returncode is None:
            process.terminate()
            with anyio.move_on_after(5):
                await process.wait()
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(5):
                await process.wait()
