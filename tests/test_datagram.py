"""Tests for the datagram sockets shared by statsd and the QUIC server."""

from __future__ import annotations

import socket
import sys

import pytest

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
