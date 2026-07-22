"""Shared fixtures for anycorn subprocess-based integration tests."""

from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from anyio.abc import Process

REPO_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def _run(args: Sequence[str]) -> AsyncIterator[Process]:
    """Run ``python -m anycorn <args>`` as a real subprocess, cleaned up on exit."""
    process = await anyio.open_process(
        [sys.executable, "-m", "anycorn", *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        process.terminate()
        with anyio.move_on_after(5):
            await process.wait()
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(5):
                await process.wait()


@pytest.fixture(name="anycorn_subprocess")
def _anycorn_subprocess() -> Callable[[Sequence[str]], AbstractAsyncContextManager[Process]]:
    """Return an async context manager that runs anycorn as a real subprocess.

    Usage: ``async with anycorn_subprocess([app_path, "--bind", ...]) as process:``.
    Used by tests that need to observe behaviour only run.py's real, synchronous
    multiprocess orchestration produces (signal handling, worker daemon status) -
    nothing driven in-process via anycorn.serve() exercises that code at all.
    """
    return _run
