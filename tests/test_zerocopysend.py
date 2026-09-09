"""Full-stack zero-copy tests: a real request served by worker_serve with os.sendfile.

These drive the real ``worker_serve`` over a loopback TCP connection and make the request
with an HTTP client, so the whole path - HTTP/1.1 parsing, the app, h11 framing and the
os.sendfile in ``_transmit_file`` - is exercised and the bytes the client receives are
asserted end to end. TCP rather than an ``AF_UNIX`` socketpair because ``os.sendfile`` does
not support the latter on macOS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.run import worker_serve
from anycorn.sendfile import have_sendfile

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(not have_sendfile, reason="os.sendfile unavailable")

HOST = "127.0.0.1"


async def _serve_and_get(app: Any) -> httpx2.Response:  # noqa: ANN401
    """Serve *app* with worker_serve on an OS-assigned port and GET once over TCP."""
    config = Config()
    # Port 0 lets the OS pick a free port; worker_serve reports the actual bind URL back
    # through task_status, which avoids both a port race and the free_tcp_port fixture (it
    # allocates over IPv6, unavailable in some sandboxes).
    config.bind = [f"{HOST}:0"]
    config.accesslog = "-"
    config.errorlog = "-"
    shutdown = anyio.Event()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as task_group:
            binds: list[str] = await task_group.start(
                lambda *, task_status: worker_serve(
                    ASGIWrapper(app),
                    config,
                    shutdown_trigger=shutdown.wait,
                    task_status=task_status,
                )
            )
            async with httpx2.AsyncClient(base_url=binds[0]) as client:
                response = await client.get("/")
            shutdown.set()
    return response


@pytest.mark.anyio
async def test_pathsend_delivers_the_file(tmp_path: Path) -> None:
    """Path send transmits the whole named file as the response body via os.sendfile."""
    payload = b"pathsend zero copy\n" * 10_000
    file_path = tmp_path / "payload.bin"
    await anyio.Path(file_path).write_bytes(payload)

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(payload)).encode())],
            }
        )
        await send({"type": "http.response.pathsend", "path": str(file_path)})

    response = await _serve_and_get(app)
    response.raise_for_status()
    assert response.content == payload


@pytest.mark.anyio
async def test_pathsend_delivers_a_byte_range(tmp_path: Path) -> None:
    """Path send with offset/count transmits just that window of the file, for range requests."""
    payload = bytes(range(256)) * 500
    file_path = tmp_path / "payload.bin"
    await anyio.Path(file_path).write_bytes(payload)
    window = payload[1000:1500]

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        assert scope["extensions"]["http.response.pathsend"] == {"ranges": True}
        await send(
            {
                "type": "http.response.start",
                "status": 206,
                "headers": [
                    (b"content-length", str(len(window)).encode()),
                    (b"content-range", b"bytes 1000-1499/128000"),
                    (b"accept-ranges", b"bytes"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.pathsend",
                "path": str(file_path),
                "offset": 1000,
                "count": len(window),
            }
        )

    response = await _serve_and_get(app)
    response.raise_for_status()
    assert response.status_code == 206  # noqa: PLR2004
    assert response.content == window


@pytest.mark.anyio
async def test_zerocopysend_delivers_a_file_descriptor(tmp_path: Path) -> None:
    """The app hands over an open descriptor and offset/count; the window is sent."""
    payload = bytes(range(256)) * 500
    file_path = tmp_path / "payload.bin"
    await anyio.Path(file_path).write_bytes(payload)
    window = payload[100:-100]

    # Opened here, not inside the app: the app runs in the server's task group, which is
    # cancelled at shutdown, and an awaited close racing that cancellation can leak the file.
    file = await anyio.Path(file_path).open("rb")
    try:

        async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
            assert "http.response.zerocopysend" in scope["extensions"]
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(len(window)).encode())],
                }
            )
            await send(
                {
                    "type": "http.response.zerocopysend",
                    "file": file,
                    "offset": 100,
                    "count": len(window),
                }
            )

        response = await _serve_and_get(app)
    finally:
        await file.aclose()

    response.raise_for_status()
    assert response.content == window
