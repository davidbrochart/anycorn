"""Tests for the datagram sockets shared by statsd and the QUIC server."""

from __future__ import annotations

import socket
import sys
from typing import cast

import anyio
import anyio.abc
import pytest

from anycorn import datagram
from anycorn.datagram import _AnyioDatagramSocket, _AsyncioDatagramSocket, wrap_datagram_socket


def _bound_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)  # noqa: FBT003
    return sock


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_asyncio_socket_releases_socket() -> None:
    """The Windows implementation must neither leak its socket nor hang after a send.

    Only reached on Windows in production, so exercise it everywhere: on the proactor
    loop `transport.close()` alone leaves the socket open whilst a write is in flight.
    """
    sock = await _AsyncioDatagramSocket.connect("127.0.0.1", 9125)
    await sock.send(b"anycorn.test:1|c")
    await sock.aclose()
    assert sock.socket.fileno() == -1


@pytest.mark.anyio
async def test_anyio_socket_releases_socket(anyio_backend_name: str) -> None:
    """The implementation used on every platform other than Windows."""
    if sys.platform == "win32" and anyio_backend_name == "asyncio":
        # This is the combination the datagram module avoids: anyio's aclose() waits for
        # a connection_lost() the proactor loop never delivers, so it would hang here
        pytest.skip("anyio's UDP aclose() hangs on the proactor event loop")

    sock = await _AnyioDatagramSocket.connect("127.0.0.1", 9125)
    await sock.send(b"anycorn.test:1|c")
    await sock.aclose()
    assert sock.socket.fileno() == -1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_asyncio_socket_roundtrip() -> None:
    """Datagrams sent to an adopted socket come back out of receive()."""
    server = await _AsyncioDatagramSocket.from_socket(_bound_socket())
    host, port = server.socket.getsockname()
    client = await _AsyncioDatagramSocket.connect(host, port)

    await client.send(b"ping")
    data, address = await server.receive()
    assert data == b"ping"

    await server.sendto(b"pong", address[0], address[1])
    echoed, _ = await client.receive()
    assert echoed == b"pong"

    await client.aclose()
    await server.aclose()


class _ScriptedAnyioSocket:
    """A minimal stand-in for anyio's UDPSocket whose receive() follows a script.

    Each scripted item is either a datagram to return or an exception to raise, which
    lets a Windows ICMP reset be reproduced on any platform without a real peer. The
    raw socket _AnyioDatagramSocket reads via extra() is never touched by these tests,
    so a sentinel stands in for it rather than a real fd that would leak.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)

    def extra(self, _attribute: object) -> object:
        return object()

    async def receive(self) -> tuple[bytes, tuple[str, int]]:
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _anyio_socket_reading(script: list) -> _AnyioDatagramSocket:
    """Wrap a scripted receive() in an _AnyioDatagramSocket, standing in for the real one."""
    return _AnyioDatagramSocket(cast("anyio.abc.UDPSocket", _ScriptedAnyioSocket(script)))


def _connection_reset() -> anyio.BrokenResourceError:
    """Build a BrokenResourceError shaped like the one anyio raises on WinError 10054."""
    error = anyio.BrokenResourceError()
    error.__cause__ = ConnectionResetError(10054, "forcibly closed by the remote host")
    return error


@pytest.mark.anyio
async def test_anyio_receive_skips_windows_connection_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer's ICMP port unreachable is dropped, not fatal, so the server reads on.

    On Windows a datagram to a departed peer surfaces as ConnectionResetError on the
    next recvfrom; the QUIC server serves every peer off one socket, so this must not
    take the whole listener down.
    """
    monkeypatch.setattr(datagram.sys, "platform", "win32")
    datagram_in = (b"next", ("127.0.0.1", 5353))
    sock = _anyio_socket_reading([_connection_reset(), datagram_in])

    assert await sock.receive() == datagram_in


@pytest.mark.anyio
async def test_anyio_receive_reraises_reset_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Windows the socket is never told about ICMP errors, so this is a real fault."""
    monkeypatch.setattr(datagram.sys, "platform", "linux")
    sock = _anyio_socket_reading([_connection_reset()])

    with pytest.raises(anyio.BrokenResourceError):
        await sock.receive()


@pytest.mark.anyio
async def test_anyio_receive_reraises_other_broken_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BrokenResourceError that is not an ICMP reset is a genuine failure even on Windows."""
    monkeypatch.setattr(datagram.sys, "platform", "win32")
    sock = _anyio_socket_reading([anyio.BrokenResourceError()])

    with pytest.raises(anyio.BrokenResourceError):
        await sock.receive()


@pytest.mark.anyio
async def test_wrap_datagram_socket_takes_ownership(anyio_backend_name: str) -> None:
    """Whichever implementation is chosen, closing it closes the adopted socket."""
    if sys.platform == "win32" and anyio_backend_name == "asyncio":
        pytest.skip("anyio's UDP aclose() hangs on the proactor event loop")

    raw = _bound_socket()
    sock = await wrap_datagram_socket(raw)
    assert sock.socket.fileno() == raw.fileno()

    await sock.aclose()
    assert raw.fileno() == -1
