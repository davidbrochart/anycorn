"""Datagram sockets that close deterministically on every platform.

anyio's UDP `aclose()` waits for a `connection_lost()` that the proactor event loop
never schedules whilst a write is in flight, so on Windows it hangs forever and leaves
the socket open (agronholm/anyio#1237). asyncio's transport exposes `abort()`, which
carries no such condition, so on Windows these drive asyncio directly; everywhere else
they are a thin wrapper over anyio.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections import deque
from typing import TYPE_CHECKING

import anyio
import anyio.abc
import sniffio
from anyio.abc import SocketAttribute

if TYPE_CHECKING:
    IPAddress = tuple[str, int]


def _needs_asyncio() -> bool:
    return sys.platform == "win32" and sniffio.current_async_library() == "asyncio"


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.read_queue: deque[tuple[bytes, IPAddress]] = deque()
        self.read_event = asyncio.Event()
        self.write_event = asyncio.Event()
        self.closed = asyncio.Event()
        self.write_event.set()

    def datagram_received(self, data: bytes, addr: IPAddress) -> None:
        self.read_queue.append((data, addr))
        self.read_event.set()

    def error_received(self, exc: Exception) -> None:
        """Ignore ICMP errors, as a datagram server cannot act on them."""

    def pause_writing(self) -> None:
        self.write_event.clear()

    def resume_writing(self) -> None:
        self.write_event.set()

    def connection_lost(self, exc: Exception | None) -> None:  # noqa: ARG002
        self.read_event.set()
        self.write_event.set()
        self.closed.set()


class _AsyncioDatagramSocket:
    """Datagram socket driven through asyncio, so that the transport can be aborted."""

    def __init__(self, transport: asyncio.DatagramTransport, protocol: _DatagramProtocol) -> None:
        self._transport = transport
        self._protocol = protocol
        self.socket: socket.socket = transport.get_extra_info("socket")

    @classmethod
    async def connect(cls, host: str, port: int) -> _AsyncioDatagramSocket:
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _DatagramProtocol, remote_addr=(host, port), family=socket.AddressFamily.AF_INET
        )
        return cls(transport, protocol)

    @classmethod
    async def from_socket(cls, sock: socket.socket) -> _AsyncioDatagramSocket:
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _DatagramProtocol, sock=sock
        )
        return cls(transport, protocol)

    async def receive(self) -> tuple[bytes, IPAddress]:
        while not self._protocol.read_queue:
            if self._protocol.closed.is_set():
                raise anyio.ClosedResourceError

            self._protocol.read_event.clear()
            await self._protocol.read_event.wait()

        return self._protocol.read_queue.popleft()

    async def send(self, data: bytes) -> None:
        await self._protocol.write_event.wait()
        self._transport.sendto(data)

    async def sendto(self, data: bytes, host: str, port: int) -> None:
        await self._protocol.write_event.wait()
        self._transport.sendto(data, (host, port))

    async def aclose(self) -> None:
        self._transport.close()
        try:
            # Give a completing write the chance to close the transport by itself
            await asyncio.sleep(0)
        finally:
            # Unlike close(), abort() always schedules connection_lost, so this must run
            # even if the checkpoint above is cancelled. Having scheduled it, the wait is
            # bounded by a single iteration of the loop, so shield it: a cancelled close
            # must still leave the socket released.
            self._transport.abort()
            with anyio.CancelScope(shield=True):
                await self._protocol.closed.wait()


class _AnyioDatagramSocket:
    """Datagram socket backed by anyio, used wherever its `aclose()` is sound.

    `connect()` leaves the socket unconnected and addresses each datagram instead. A
    connected UDP socket is told about ICMP port unreachable, so a peer that is down -
    a statsd daemon, say - surfaces as ECONNREFUSED on a later send, which anyio raises
    as `BrokenResourceError` from whichever caller happened to be sending at the time.
    """

    def __init__(self, sock: anyio.abc.UDPSocket, remote: IPAddress | None = None) -> None:
        self._socket = sock
        self._remote = remote
        self.socket: socket.socket = sock.extra(SocketAttribute.raw_socket)  # noqa: S610

    @classmethod
    async def connect(cls, host: str, port: int) -> _AnyioDatagramSocket:
        return cls(await anyio.create_udp_socket(family=socket.AddressFamily.AF_INET), (host, port))

    @classmethod
    async def from_socket(cls, sock: socket.socket) -> _AnyioDatagramSocket:
        return cls(await anyio.abc.UDPSocket.from_socket(sock))

    async def receive(self) -> tuple[bytes, IPAddress]:
        while True:
            try:
                return await self._socket.receive()
            except anyio.BrokenResourceError as error:  # noqa: PERF203
                # On Windows a datagram sent to a peer that has gone away comes back as
                # an ICMP port unreachable, and the socket reports it as a
                # ConnectionResetError (WinError 10054) on the *next* recvfrom, which
                # anyio raises as BrokenResourceError. A datagram server serves many
                # peers off one socket and cannot act on one peer's ICMP error, so drop
                # it and read the next datagram - exactly as the asyncio path's
                # error_received does. The error is delivered once, so the retry then
                # blocks for real traffic rather than spinning. Elsewhere the socket is
                # not told about ICMP errors, so nothing raises this and the loop runs
                # once.
                if sys.platform != "win32" or not isinstance(error.__cause__, ConnectionResetError):
                    raise

    async def send(self, data: bytes) -> None:
        assert self._remote is not None
        host, port = self._remote
        await self._socket.sendto(data, host, port)

    async def sendto(self, data: bytes, host: str, port: int) -> None:
        await self._socket.sendto(data, host, port)

    async def aclose(self) -> None:
        await self._socket.aclose()


DatagramSocket = _AsyncioDatagramSocket | _AnyioDatagramSocket


async def connect_datagram_socket(host: str, port: int) -> DatagramSocket:
    """Open a datagram socket connected to the given address."""
    if _needs_asyncio():
        return await _AsyncioDatagramSocket.connect(host, port)

    return await _AnyioDatagramSocket.connect(host, port)


async def wrap_datagram_socket(sock: socket.socket) -> DatagramSocket:
    """Adopt an existing datagram socket, taking ownership of it."""
    if _needs_asyncio():
        return await _AsyncioDatagramSocket.from_socket(sock)

    return await _AnyioDatagramSocket.from_socket(sock)
