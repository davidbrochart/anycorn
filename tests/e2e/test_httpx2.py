"""End-to-end tests using httpx2."""

from __future__ import annotations

from dataclasses import dataclass
from ssl import SSLContext, create_default_context
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest

import anycorn
from anycorn.config import Config

if TYPE_CHECKING:
    from tests.conftest import TLSCerts

HOST = "127.0.0.1"
TLS_HOST = "localhost"


@dataclass(frozen=True)
class HTTPX2Mode:
    free_tcp_port: int
    tls_certs: TLSCerts
    protocol: str
    http2: bool

    def configure(self, config: Config) -> None:
        config.bind = [f"{HOST}:{self.free_tcp_port}"]
        if self.protocol == "http2":
            config.certfile = str(self.tls_certs.certfile)
            config.keyfile = str(self.tls_certs.keyfile)
            config.alpn_protocols = ["h2", "http/1.1"]
        elif self.protocol == "http1_tls":
            config.certfile = str(self.tls_certs.certfile)
            config.keyfile = str(self.tls_certs.keyfile)
            config.alpn_protocols = ["http/1.1"]

    def base_url(self) -> str:
        if self.protocol == "http1":
            return f"http://{HOST}:{self.free_tcp_port}"
        return f"https://{TLS_HOST}:{self.free_tcp_port}"

    def verify(self) -> bool | SSLContext:
        if self.protocol == "http1":
            return True
        return create_default_context(cafile=str(self.tls_certs.cafile))

    def async_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self.base_url(),
            http2=self.http2,
            verify=self.verify(),
        )


@pytest.fixture(
    params=[
        pytest.param(("http2", True), id="http2"),
        pytest.param(("http1", False), id="http1"),
        pytest.param(("http1_tls", False), id="http1-tls"),
    ]
)
def httpx2_mode(
    request: pytest.FixtureRequest,
    tls_certs: TLSCerts,
    free_tcp_port: int,
) -> HTTPX2Mode:
    protocol, http2 = request.param
    return HTTPX2Mode(
        free_tcp_port=free_tcp_port,
        tls_certs=tls_certs,
        protocol=protocol,
        http2=http2,
    )


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
    config.bind = [f"{HOST}:{free_tcp_port}"]
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

        async with httpx2.AsyncClient(base_url=f"http://{HOST}:{free_tcp_port}") as client:
            # Make sure that we properly clean up connections when `keep_alive_max_requests`
            # is hit such that the client stays good over multiple hangups.
            for _ in range(10):
                result = await client.post("/test", json={"key": "value"})
                result.raise_for_status()

        shutdown.set()


@pytest.mark.anyio
async def test_server_cancelled_mid_request(
    httpx2_mode: HTTPX2Mode,
) -> None:
    config = Config()
    httpx2_mode.configure(config)
    config.accesslog = "-"
    config.errorlog = "-"

    started = anyio.Event()
    request_finished = anyio.Event()
    request_error: httpx2.TransportError | None = None
    shutdown = anyio.Event()

    async def hanging_app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
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
            response = await client.get("/")
            await response.aread()
        except httpx2.TransportError as error:
            request_error = error
        finally:
            request_finished.set()

    async with anyio.create_task_group() as tg:
        await tg.start(
            lambda *, task_status: anycorn.serve(
                hanging_app,
                config,
                shutdown_trigger=shutdown.wait,
                task_status=task_status,
            )
        )

        async with httpx2_mode.async_client() as client:
            tg.start_soon(make_request, client)
            with anyio.fail_after(10):
                await started.wait()

            shutdown.set()

            with anyio.fail_after(10):
                await request_finished.wait()

    assert request_error is not None
    if httpx2_mode.http2:
        assert (
            "Server disconnected" in str(request_error)
            or "socket connection broken" in str(request_error)
        )
    else:
        assert "peer closed connection without sending complete message body" in str(request_error)
