"""Tests for TCPServer connection teardown."""

from __future__ import annotations

import errno

import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.tcp_server import TCPServer
from anycorn.worker_context import WorkerContext

from .helpers import empty_framework


class _UnreachableStream:
    """A stream whose teardown raises a network-unreachable OSError.

    asyncio leaves that errno unmapped, as an abrupt client disconnect (a deleted
    pod, a pulled cable) can produce.
    """

    def __init__(self) -> None:
        self.aclose_called = False

    async def send_eof(self) -> None:
        pass

    async def aclose(self) -> None:
        self.aclose_called = True
        raise OSError(errno.EHOSTUNREACH, "No route to host")


@pytest.mark.anyio
async def test_close_suppresses_host_unreachable_oserror() -> None:
    """_close() must swallow a plain OSError from the stream, not let it escape.

    asyncio maps only a handful of errno (ECONNRESET, EPIPE, ...) to ConnectionError;
    EHOSTUNREACH and its kin (ENETUNREACH, ETIMEDOUT) stay plain OSError. _close()
    runs from run()'s finally - outside its own `except OSError` - and from
    protocol_send, so an escaping OSError crashes the connection task or propagates
    back into the ASGI app (https://github.com/pgjones/hypercorn/issues/361).
    """
    stream = _UnreachableStream()
    server = TCPServer(
        ASGIWrapper(empty_framework),
        Config(),
        WorkerContext(None),
        {},
        stream,  # type: ignore[arg-type]
    )

    await server._close()  # must not raise

    assert stream.aclose_called
