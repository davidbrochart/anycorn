"""Tests for the statsd logger's UDP senders."""

from __future__ import annotations

import socket
import sys

import anyio
import pytest
from anyio.abc import SocketAttribute

from anycorn.config import Config
from anycorn.statsd import StatsdLogger, _AnyioSender, _AsyncioSender


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
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_asyncio_sender_releases_socket() -> None:
    """The Windows sender must neither leak its socket nor hang after a send.

    Only reached on Windows in production, so exercise it everywhere: on the proactor
    loop `transport.close()` alone leaves the socket open whilst a write is in flight.
    """
    sender = await _AsyncioSender.create("127.0.0.1", 9125)
    await sender.send(b"anycorn.test:1|c")
    await sender.aclose()
    assert sender.socket.fileno() == -1


@pytest.mark.anyio
async def test_anyio_sender_releases_socket(anyio_backend_name: str) -> None:
    """The sender used on every platform other than Windows."""
    if sys.platform == "win32" and anyio_backend_name == "asyncio":
        # This is the combination StatsdLogger avoids: anyio's aclose() waits for a
        # connection_lost() the proactor loop never delivers, so it would hang here
        pytest.skip("anyio's UDP aclose() hangs on the proactor event loop")

    sender = await _AnyioSender.create("127.0.0.1", 9125)
    await sender.send(b"anycorn.test:1|c")
    await sender.aclose()
    assert sender.socket.fileno() == -1


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
