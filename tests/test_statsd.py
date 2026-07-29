"""Tests for the statsd logger."""

from __future__ import annotations

import socket

import anyio
import anyio.lowlevel
import pytest
from anyio.abc import SocketAttribute

from anycorn.config import Config
from anycorn.statsd import StatsdLogger


def _unused_udp_port() -> int:
    """Return a port with nothing bound to it, so datagrams are refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.anyio
async def test_metrics_reach_the_statsd_daemon() -> None:
    """The datagrams a sender emits must arrive, addressed at an actual listener."""
    async with await anyio.create_udp_socket(local_host="127.0.0.1") as daemon:
        _, port = daemon.extra(SocketAttribute.local_address)  # noqa: S610

        config = Config()
        config.statsd_host = f"127.0.0.1:{port}"
        logger = StatsdLogger(config)
        try:
            await logger.increment("anycorn.test", 1)
            with anyio.fail_after(5):
                message, _ = await daemon.receive()
        finally:
            await logger.aclose()

    assert message == b"anycorn.test:1|c|@1.0"


@pytest.mark.anyio
async def test_metrics_survive_a_daemon_that_is_not_listening() -> None:
    """A statsd daemon that is down must not break the request being measured.

    A connected UDP socket is told about ICMP port unreachable, so the send after the
    one that provoked it fails - taking down whichever request happened to be logging.
    """
    config = Config()
    config.statsd_host = f"127.0.0.1:{_unused_udp_port()}"
    logger = StatsdLogger(config)
    try:
        for _ in range(5):
            await logger.increment("anycorn.test", 1)
            await anyio.sleep(0.01)
    finally:
        await logger.aclose()


@pytest.mark.anyio
async def test_concurrent_first_sends_open_one_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two coroutines emitting their first metric together must share one socket.

    The lazy open awaits, so without a lock both callers pass the ``is None`` check
    and each open a socket, orphaning all but one.
    """
    opened = 0

    class _FakeSender:
        async def send(self, message: bytes) -> None:
            pass

        async def aclose(self) -> None:
            pass

    async def fake_connect(_host: str, _port: int) -> _FakeSender:
        nonlocal opened
        opened += 1
        await anyio.lowlevel.checkpoint()  # checkpoint: lets a second caller interleave
        return _FakeSender()

    monkeypatch.setattr("anycorn.statsd.connect_datagram_socket", fake_connect)

    config = Config()
    config.statsd_host = "127.0.0.1:9125"
    logger = StatsdLogger(config)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(logger.increment, "anycorn.a", 1)
        task_group.start_soon(logger.increment, "anycorn.b", 1)

    assert opened == 1


@pytest.mark.anyio
async def test_aclose_forcefully_releases_socket() -> None:
    """Closing in a cancelled scope must still release the socket."""
    config = Config()
    config.statsd_host = "localhost:9125"
    logger = StatsdLogger(config)
    await logger.increment("anycorn.test", 1)
    sender = logger._sender
    assert sender is not None

    await anyio.aclose_forcefully(logger)

    assert sender.socket.fileno() == -1
