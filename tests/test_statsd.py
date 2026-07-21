"""Tests for the statsd logger's UDP senders."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, cast

import anyio
import pytest
from anyio.abc import SocketAttribute

from anycorn.config import Config
from anycorn.statsd import StatsdLogger, _AnyioSender, _AsyncioSender

if TYPE_CHECKING:
    import socket


def _raw_socket(logger: StatsdLogger) -> socket.socket:
    sender = logger._sender
    if isinstance(sender, _AsyncioSender):
        return cast("socket.socket", sender._transport.get_extra_info("socket"))

    assert isinstance(sender, _AnyioSender)
    return cast("socket.socket", sender._socket.extra(SocketAttribute.raw_socket))  # noqa: S610


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_asyncio_sender_releases_socket() -> None:
    """The Windows sender must neither leak its socket nor hang after a send.

    Only reached on Windows in production, so exercise it everywhere: on the proactor
    loop `transport.close()` alone leaves the socket open whilst a write is in flight.
    """
    sender = await _AsyncioSender.create("127.0.0.1", 9125)
    raw = cast("socket.socket", sender._transport.get_extra_info("socket"))
    await sender.send(b"anycorn.test:1|c")
    await sender.aclose()
    assert raw.fileno() == -1


@pytest.mark.anyio
async def test_anyio_sender_releases_socket(anyio_backend_name: str) -> None:
    """The sender used on every platform other than Windows."""
    if sys.platform == "win32" and anyio_backend_name == "asyncio":
        # This is the combination StatsdLogger avoids: anyio's aclose() waits for a
        # connection_lost() the proactor loop never delivers, so it would hang here
        pytest.skip("anyio's UDP aclose() hangs on the proactor event loop")

    sender = await _AnyioSender.create("127.0.0.1", 9125)
    raw = cast("socket.socket", sender._socket.extra(SocketAttribute.raw_socket))  # noqa: S610
    await sender.send(b"anycorn.test:1|c")
    await sender.aclose()
    assert raw.fileno() == -1


@pytest.mark.anyio
async def test_aclose_forcefully_releases_socket() -> None:
    """Closing in a cancelled scope must still release the socket."""
    config = Config()
    config.statsd_host = "localhost:9125"
    logger = StatsdLogger(config)
    await logger.increment("anycorn.test", 1)
    raw = _raw_socket(logger)

    await anyio.aclose_forcefully(logger)

    assert raw.fileno() == -1
