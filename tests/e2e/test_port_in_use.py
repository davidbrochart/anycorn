"""Integration test: a second anycorn server on a busy port must fail to start.

On Windows the listening socket used SO_REUSEADDR, which lets an unrelated socket
rebind an address already in use - so a second `anycorn` started on a port already
being served bound silently and stole it instead of failing. This runs two real
`python -m anycorn` processes: the first serves a port, the second is launched on
the same port and must exit non-zero rather than come up alongside it.

https://github.com/pgjones/hypercorn/issues/171
"""

from __future__ import annotations

import anyio
import httpx2
import pytest

from tests.e2e._subprocess import anycorn_subprocess

APP = "tests/assets/pid_app.py:app"


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
async def test_second_server_on_a_used_port_exits_nonzero(
    anyio_backend_name: str, free_tcp_port: int
) -> None:
    """A second server on a port already served must refuse to start.

    https://github.com/pgjones/hypercorn/issues/171
    """
    args = [APP, "--bind", f"127.0.0.1:{free_tcp_port}", "--workers", "1"]
    base_url = f"http://127.0.0.1:{free_tcp_port}"

    async with anycorn_subprocess(args, anyio_backend_name=anyio_backend_name):
        await _wait_until_serving(base_url)

        # The second server binds the same port in its parent process, so a refused
        # bind surfaces as a non-zero exit rather than a running, port-stealing server.
        async with anycorn_subprocess(args, anyio_backend_name=anyio_backend_name) as second:
            with anyio.fail_after(15):
                returncode = await second.wait()

    assert returncode != 0
