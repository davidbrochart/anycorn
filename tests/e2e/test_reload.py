"""Integration test for the reloader's exit status on a failed reload.

hypercorn #269: `--reload` onto a Python SyntaxError exited 0, so a supervisor or
CI could not tell the reload had failed. Two things had to line up - run() must
carry the crashed worker's non-zero code out of its supervise loop instead of
re-joining an already-emptied process list, and the click entrypoint must exit
with that code rather than discard it (click runs the command in standalone mode).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import anyio
import httpx2
import pytest

if TYPE_CHECKING:
    from pathlib import Path

_GOOD_APP = (
    "async def app(scope, receive, send):\n"
    "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
    "    await send({'type': 'http.response.body', 'body': b'ok'})\n"
)
_BROKEN_APP = "async def app(scope, receive, send)  # <- SyntaxError: missing colon\n    pass\n"


async def _is_serving(base_url: str) -> bool:
    try:
        async with httpx2.AsyncClient(base_url=base_url) as client:
            await client.get("/")
    except httpx2.TransportError:
        return False
    else:
        return True


async def _wait_until_serving(base_url: str) -> None:
    with anyio.fail_after(15):
        while True:
            if await _is_serving(base_url):
                return
            await anyio.sleep(0.1)


@pytest.mark.anyio
async def test_reload_onto_a_syntax_error_exits_nonzero(
    anyio_backend_name: str, free_tcp_port: int, tmp_path: Path
) -> None:
    """A reload onto an unimportable module must fail the process, not exit 0."""
    app_file = tmp_path / "reload_app.py"
    app_file.write_text(_GOOD_APP)

    # cwd is on sys.path for `python -m`, so the app in tmp_path is importable, while
    # anycorn itself still resolves from the environment.
    process = await anyio.open_process(
        [
            sys.executable,
            "-m",
            "anycorn",
            "--reload",
            "--bind",
            f"127.0.0.1:{free_tcp_port}",
            "--workers",
            "1",
            "--worker-class",
            anyio_backend_name,
            "reload_app:app",
        ],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_until_serving(f"http://127.0.0.1:{free_tcp_port}")

        # Break the module, pushing its mtime forward so the ~1s poller cannot miss it.
        app_file.write_text(_BROKEN_APP)
        stamp = app_file.stat().st_mtime + 10
        os.utime(app_file, (stamp, stamp))

        with anyio.fail_after(30):
            returncode = await process.wait()

        assert returncode != 0
    finally:
        # terminate()/kill() on an already-reaped process raises under asyncio, so
        # guard each call on the process still running rather than assuming it is.
        if process.returncode is None:
            process.terminate()
            with anyio.move_on_after(5):
                await process.wait()
        if process.returncode is None:
            process.kill()
