"""End-to-end HTTP/3 tests, driven by aioquic's client."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration

import anycorn
from anycorn.config import Config

if TYPE_CHECKING:
    import asyncio

    from aioquic.quic.events import QuicEvent

ASSETS = Path(__file__).parent.parent / "assets"
BIND = "127.0.0.1:4433"


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"Hello, h3!"})


class _H3Client(QuicConnectionProtocol):
    """The smallest HTTP/3 client that can make one request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)
        self._body = bytearray()
        self._headers: list[tuple[bytes, bytes]] = []
        self._done: asyncio.Future[None] = self._loop.create_future()

    def quic_event_received(self, event: QuicEvent) -> None:
        for h3_event in self._http.handle_event(event):
            if isinstance(h3_event, HeadersReceived):
                self._headers = h3_event.headers
            elif isinstance(h3_event, DataReceived):
                self._body.extend(h3_event.data)

            if getattr(h3_event, "stream_ended", False) and not self._done.done():
                self._done.set_result(None)

    async def get(self, path: str) -> tuple[list[tuple[bytes, bytes]], bytes]:
        stream_id = self._quic.get_next_available_stream_id()
        self._http.send_headers(
            stream_id,
            [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", BIND.encode()),
                (b":path", path.encode()),
            ],
            end_stream=True,
        )
        self.transmit()
        await self._done
        return self._headers, bytes(self._body)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])  # aioquic's client is asyncio only
async def test_h3_request() -> None:
    config = Config()
    config.bind = [BIND]
    config.quic_bind = [BIND]
    config.certfile = str(ASSETS / "cert.pem")
    config.keyfile = str(ASSETS / "key.pem")
    config.accesslog = "-"
    config.errorlog = "-"

    client_config = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    client_config.verify_mode = ssl.CERT_NONE

    shutdown = anyio.Event()
    async with anyio.create_task_group() as tg:
        binds = await tg.start(
            lambda *, task_status: anycorn.serve(
                app, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )
        assert any("4433" in bind for bind in binds)

        async with connect(
            "127.0.0.1", 4433, configuration=client_config, create_protocol=_H3Client
        ) as connection:
            client = cast("_H3Client", connection)
            with anyio.fail_after(10):
                headers, body = await client.get("/")

        shutdown.set()

    assert dict(headers)[b":status"] == b"200"
    assert body == b"Hello, h3!"
