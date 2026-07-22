"""Tests for worker startup and socket lifecycle."""

from __future__ import annotations

import signal
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
    from collections.abc import Callable

    from tests.conftest import TLSCerts


async def app(scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class _FakeSignalModule:
    """Stands in for the real ``signal`` module, scoped to run.py's own namespace.

    Any test that drives run() into its multiprocess branch needs this: the very
    first thing that branch does, before calling _populate or anything else, is
    a real ``signal.signal(SIGINT, SIG_IGN)``. Replacing the real, process-wide
    module's ``.signal`` attribute would intercept calls from unrelated code
    running during the test too, and a signal handler actually installed on the
    real module would outlive monkeypatch's undo (it only reverts attribute
    assignments, not OS-level signal state). Rebinding the ``signal`` name
    inside anycorn.run's own namespace avoids both: the real module, and every
    other test, are never touched.
    """

    SIGHUP = signal.SIGHUP
    SIGINT = signal.SIGINT
    SIGTERM = signal.SIGTERM
    SIG_IGN = signal.SIG_IGN

    def __init__(self) -> None:
        self.calls: list[tuple[int, Callable]] = []

    def signal(self, signalnum: int, handler: Callable) -> None:
        self.calls.append((signalnum, handler))


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


def test_populate_sets_process_daemon_from_config() -> None:
    """Each spawned worker process must pick up config.daemon, not a hardcoded value."""

    class _Process:
        def __init__(self, target: Any, kwargs: dict) -> None:  # noqa: ANN401
            self.target = target
            self.kwargs = kwargs
            self.daemon = False

        def start(self) -> None:
            pass

    class _Ctx:
        def Process(self, *, target: Any, kwargs: dict) -> _Process:  # noqa: ANN401, N802
            return _Process(target, kwargs)

    config = Config()
    config.daemon = False
    processes: list = []
    anycorn.run._populate(processes, config, lambda **_kwargs: None, None, None, _Ctx())  # type: ignore[arg-type]
    assert processes[0].daemon is False


def test_run_registers_sighup_to_reload_workers(
    tls_certs: TLSCerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGHUP must be wired up to gracefully restart workers, not left to the default action."""
    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.certfile = str(tls_certs.certfile)
    config.keyfile = str(tls_certs.keyfile)
    sockets = config.create_sockets()
    monkeypatch.setattr(config, "create_sockets", lambda: sockets)
    config.workers = 1

    class _Process:
        def __init__(self) -> None:
            self.sentinel = object()
            self.exitcode = 1  # non-zero, so run() stops after a single pass

        def join(self) -> None:
            pass

        def terminate(self) -> None:
            pass

    def _populate(processes: list, *_args: object, **_kwargs: object) -> None:
        if not processes:
            processes.append(_Process())

    fake_signal = _FakeSignalModule()

    monkeypatch.setattr(anycorn.run, "_populate", _populate)
    monkeypatch.setattr(anycorn.run, "wait", lambda _sentinels: None)
    monkeypatch.setattr(anycorn.run, "signal", fake_signal)

    run(config)

    assert signal.signal is not fake_signal.signal  # the real module was never touched

    sighup_handlers = [
        handler for signalnum, handler in fake_signal.calls if signalnum == signal.SIGHUP
    ]
    assert len(sighup_handlers) == 1
    assert getattr(sighup_handlers[0], "__name__", None) == "reload"


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

    fake_signal = _FakeSignalModule()

    monkeypatch.setattr(anycorn.run, "_populate", _spawn_then_fail)
    monkeypatch.setattr(anycorn.run, "signal", fake_signal)

    with pytest.raises(PicklingError):
        run(config)

    assert signal.signal is not fake_signal.signal  # the real module was never touched
    assert len(terminated) == 1
