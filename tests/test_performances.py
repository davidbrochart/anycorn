import subprocess

import httpx
import pytest
import sniffio
from anyio import CancelScope, create_task_group, sleep

from .helpers import ensure_server_running

pytestmark = pytest.mark.anyio


async def wait(seconds: int, cancel_scope: CancelScope, proc: subprocess.Popen) -> None:
    await sleep(seconds)
    cancel_scope.cancel()
    proc.terminate()
    proc.wait()


@pytest.mark.parametrize("server", ["anycorn", "hypercorn"])
async def test_http_performances(server: str, unused_tcp_port: int) -> None:
    backend = sniffio.current_async_library()
    host = "127.0.0.1"
    url = f"http://{host}:{unused_tcp_port}"
    get_nb = 0
    proc = subprocess.Popen(  # noqa: ASYNC101
        ["python", "tests/run_server.py", host, str(unused_tcp_port), backend, server]
    )
    try:
        async with create_task_group() as tg:
            await ensure_server_running(host, unused_tcp_port)
            tg.start_soon(wait, 1, tg.cancel_scope, proc)
            async with httpx.AsyncClient() as client:
                get = client.get
                while True:
                    await get(url)
                    get_nb += 1
    except Exception as exc:
        print(exc)

    print("HTTP GETs:", get_nb)
