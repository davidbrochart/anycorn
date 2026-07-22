"""Tests for worker startup and socket lifecycle."""

from __future__ import annotations

import socket
from functools import partial
from pickle import PicklingError
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import anyio
import pytest

import anycorn.run
from anycorn.config import Config
from anycorn.datagram import wrap_datagram_socket
from anycorn.events import RawData
from anycorn.run import run, worker_serve
from anycorn.udp_server import UDPServer
from anycorn.utils import wrap_app
from anycorn.worker_context import WorkerContext

if TYPE_CHECKING:
    from tests.conftest import TLSCerts


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.anyio
async def test_worker_serve_closes_quic_sockets(tls_certs: TLSCerts) -> None:
    """QUIC sockets are opened by create_sockets(), so the worker must close them."""
    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.quic_bind = ["127.0.0.1:0"]
    config.certfile = str(tls_certs.certfile)
    config.keyfile = str(tls_certs.keyfile)

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


def test_run_closes_sockets_when_it_raises(
    tls_certs: TLSCerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent's sockets are closed however run() exits, not only when it finishes."""
    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.quic_bind = ["127.0.0.1:0"]
    config.certfile = str(tls_certs.certfile)
    config.keyfile = str(tls_certs.keyfile)
    sockets = config.create_sockets()
    monkeypatch.setattr(config, "create_sockets", lambda: sockets)

    # The earliest thing run() refuses to do, so it raises with the sockets already open
    config.use_reloader = True
    config.workers = 0
    with pytest.raises(RuntimeError, match="Cannot reload without workers"):
        run(config)

    opened = [*sockets.secure_sockets, *sockets.insecure_sockets, *sockets.quic_sockets]
    assert opened, "expected create_sockets to bind something"
    assert [sock.fileno() for sock in opened] == [-1] * len(opened)


def test_run_terminates_workers_when_it_raises(
    tls_certs: TLSCerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that fails partway signals its workers rather than orphaning them."""
    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.certfile = str(tls_certs.certfile)
    config.keyfile = str(tls_certs.keyfile)
    sockets = config.create_sockets()
    monkeypatch.setattr(config, "create_sockets", lambda: sockets)
    config.workers = 2

    terminated: list[object] = []

    class _Process:
        sentinel = None
        exitcode = None

        def terminate(self) -> None:
            terminated.append(self)

    def _spawn_then_fail(processes: list, *_args: object, **_kwargs: object) -> None:
        # One worker up, the next refusing - the shape that used to walk past the
        # terminate loop entirely
        processes.append(_Process())
        raise PicklingError

    monkeypatch.setattr(anycorn.run, "_populate", _spawn_then_fail)

    with pytest.raises(PicklingError):
        run(config)

    assert len(terminated) == 1
