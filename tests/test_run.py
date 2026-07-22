"""Tests for worker startup and socket lifecycle."""

from __future__ import annotations

import socket
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from anycorn.config import Config
from anycorn.datagram import wrap_datagram_socket
from anycorn.events import RawData
from anycorn.run import worker_serve
from anycorn.udp_server import UDPServer
from anycorn.utils import wrap_app
from anycorn.worker_context import WorkerContext

ASSETS = Path(__file__).parent / "assets"


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.anyio
async def test_worker_serve_closes_quic_sockets() -> None:
    """QUIC sockets are opened by create_sockets(), so the worker must close them."""
    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.quic_bind = ["127.0.0.1:0"]
    config.certfile = str(ASSETS / "cert.pem")
    config.keyfile = str(ASSETS / "key.pem")

    sockets = config.create_sockets()
    quic_sockets = list(sockets.quic_sockets)
    assert quic_sockets, "expected create_sockets to bind a QUIC socket"
    for sock in sockets.secure_sockets:
        sock.listen(config.backlog)

    shutdown = anyio.Event()
    async with anyio.create_task_group() as tg:
        binds = await tg.start(
            partial(
                worker_serve,
                wrap_app(app, config.wsgi_max_body_size, None),
                config,
                sockets=sockets,
                shutdown_trigger=shutdown.wait,
            )
        )
        assert len(binds) == len(sockets.secure_sockets) + len(quic_sockets)
        shutdown.set()

    assert [sock.fileno() for sock in quic_sockets] == [-1] * len(quic_sockets)


@pytest.mark.anyio
async def test_udp_server_serialises_concurrent_sends() -> None:
    """QUIC sends from several tasks at once must not collide on the socket.

    anyio guards a socket against concurrent writers rather than interleaving them,
    so the timer and stream tasks sending alongside the read loop would otherwise
    raise `BusyResourceError` at whichever one lost the race.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)  # noqa: FBT003
    datagram_socket = await wrap_datagram_socket(sock)
    server = UDPServer(AsyncMock(), Config(), WorkerContext(None), {}, datagram_socket)

    try:
        async with anyio.create_task_group() as task_group:
            for _ in range(20):
                task_group.start_soon(
                    server.protocol_send,
                    RawData(data=b"x" * 1024, address=("127.0.0.1", 9999)),
                )
    finally:
        await datagram_socket.aclose()
