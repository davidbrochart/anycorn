"""StatsD logger integration for Anycorn."""

from __future__ import annotations

import asyncio
import socket
import sys
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import sniffio
from anyio.abc import SocketAttribute

from .logging import Logger

if TYPE_CHECKING:
    from .config import Config
    from .typing import ResponseSummary, WWWScope

METRIC_VAR = "metric"
VALUE_VAR = "value"
MTYPE_VAR = "mtype"
GAUGE_TYPE = "gauge"
COUNTER_TYPE = "counter"
HISTOGRAM_TYPE = "histogram"


class BaseStatsdLogger(Logger):
    """Logger that additionally emits metrics to a StatsD endpoint."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.dogstatsd_tags = config.dogstatsd_tags
        self.prefix = config.statsd_prefix
        if len(self.prefix) and self.prefix[-1] != ".":
            self.prefix += "."

    async def critical(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a critical message and increment the critical metric."""
        await super().critical(message, *args, **kwargs)
        await self.increment("anycorn.log.critical", 1)

    async def error(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an error message and increment the error metric."""
        await super().error(message, *args, **kwargs)
        await self.increment("anycorn.log.error", 1)

    async def warning(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a warning message and increment the warning metric."""
        await super().warning(message, *args, **kwargs)
        await self.increment("anycorn.log.warning", 1)

    async def info(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an info message."""
        await super().info(message, *args, **kwargs)

    async def debug(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a debug message."""
        await super().debug(message, *args, **kwargs)

    async def exception(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an exception and increment the exception metric."""
        await super().exception(message, *args, **kwargs)
        await self.increment("anycorn.log.exception", 1)

    async def log(self, level: int, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a message at the given level, optionally emitting a StatsD metric."""
        try:
            extra = kwargs.get("extra")
            if extra is not None:
                metric = extra.get(METRIC_VAR, None)
                value = extra.get(VALUE_VAR, None)
                type_ = extra.get(MTYPE_VAR, None)
                if metric and value and type_:
                    if type_ == GAUGE_TYPE:
                        await self.gauge(metric, value)
                    elif type_ == COUNTER_TYPE:
                        await self.increment(metric, value)
                    elif type_ == HISTOGRAM_TYPE:
                        await self.histogram(metric, value)

            if message:
                await super().log(level, message, *args, **kwargs)
        except Exception:  # noqa: BLE001
            await super().warning("Failed to log to statsd", exc_info=True)

    async def access(
        self, request: WWWScope, response: ResponseSummary | None, request_time: float
    ) -> None:
        """Log an access entry and emit request duration/count metrics."""
        if response is not None:
            await super().access(request, response, request_time)
        await self.histogram("anycorn.request.duration", request_time * 1_000)
        await self.increment("anycorn.requests", 1)
        if response is not None:
            await self.increment(f"anycorn.request.status.{response['status']}", 1)

    async def gauge(self, name: str, value: int) -> None:
        """Send a gauge metric."""
        await self._send(f"{self.prefix}{name}:{value}|g")

    async def increment(self, name: str, value: int, sampling_rate: float = 1.0) -> None:
        """Send a counter increment metric."""
        await self._send(f"{self.prefix}{name}:{value}|c|@{sampling_rate}")

    async def decrement(self, name: str, value: int, sampling_rate: float = 1.0) -> None:
        """Send a counter decrement metric."""
        await self._send(f"{self.prefix}{name}:-{value}|c|@{sampling_rate}")

    async def histogram(self, name: str, value: float) -> None:
        """Send a histogram/timing metric."""
        await self._send(f"{self.prefix}{name}:{value}|ms")

    async def _send(self, message: str) -> None:
        if self.dogstatsd_tags:
            message = f"{message}|#{self.dogstatsd_tags}"
        await self._socket_send(message.encode("ascii"))

    async def _socket_send(self, message: bytes) -> None:
        raise NotImplementedError


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.write_event = asyncio.Event()
        self.write_event.set()

    def pause_writing(self) -> None:
        self.write_event.clear()

    def resume_writing(self) -> None:
        self.write_event.set()

    def connection_lost(self, exc: Exception | None) -> None:  # noqa: ARG002
        self.write_event.set()
        self.closed.set()


class _AsyncioSender:
    """Sends datagrams via asyncio, so that the transport can be aborted on close.

    Used only on Windows, where the proactor event loop will not schedule
    `connection_lost()` from `transport.close()` whilst a write is in flight - leaving
    the socket unclosed and anyio's `aclose()`, which waits for that event, hanging
    forever. `abort()` carries no such condition, and anyio's UDP wrapper offers no way
    to reach it. Every other platform uses `_AnyioSender`.
    see https://github.com/agronholm/anyio/issues/1237
    """

    def __init__(self, transport: asyncio.DatagramTransport, protocol: _DatagramProtocol) -> None:
        self._transport = transport
        self._protocol = protocol
        self.socket: socket.socket = transport.get_extra_info("socket")

    @classmethod
    async def create(cls, host: str, port: int) -> _AsyncioSender:
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _DatagramProtocol,
            remote_addr=(host, port),
            family=socket.AddressFamily.AF_INET,
        )
        return cls(transport, protocol)

    async def send(self, message: bytes) -> None:
        # Datagram transports have no drain, so this is the transport's high/low water
        # mark flow control, matching what anyio's own send() waits on
        await self._protocol.write_event.wait()
        self._transport.sendto(message)

    async def aclose(self) -> None:
        self._transport.close()
        try:
            # Give a completing write the chance to close the transport by itself
            await asyncio.sleep(0)
        finally:
            # Unlike close(), abort() always schedules connection_lost, so this must
            # run even if the checkpoint above is cancelled. Having scheduled it, the
            # wait is bounded by a single iteration of the loop, so shield it: a
            # cancelled close must still leave the socket released.
            self._transport.abort()
            with anyio.CancelScope(shield=True):
                await self._protocol.closed.wait()


class _AnyioSender:
    """Sends datagrams via anyio, for backends other than asyncio.

    The socket is deliberately left unconnected. A connected UDP socket is told about
    ICMP port unreachable, surfacing a statsd daemon that is down as ECONNREFUSED on a
    later send - which anyio raises as `BrokenResourceError`, from whichever request
    happened to be logging at the time. Metrics are best effort and must not take the
    request being measured down with them, so address each datagram instead.
    """

    def __init__(self, sock: anyio.abc.UDPSocket, host: str, port: int) -> None:
        self._socket = sock
        self._host = host
        self._port = port
        self.socket: socket.socket = sock.extra(SocketAttribute.raw_socket)  # noqa: S610

    @classmethod
    async def create(cls, host: str, port: int) -> _AnyioSender:
        return cls(await anyio.create_udp_socket(family=socket.AddressFamily.AF_INET), host, port)

    async def send(self, message: bytes) -> None:
        await self._socket.sendto(message, self._host, self._port)

    async def aclose(self) -> None:
        await self._socket.aclose()


class StatsdLogger(BaseStatsdLogger):
    """StatsD logger that sends metrics over UDP."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        assert config.statsd_host is not None
        self.address = tuple(config.statsd_host.rsplit(":", 1))
        self._sender: _AsyncioSender | _AnyioSender | None = None

    async def _socket_send(self, message: bytes) -> None:
        if self._sender is None:
            host, port = self.address[0], int(self.address[1])
            if sys.platform == "win32" and sniffio.current_async_library() == "asyncio":
                self._sender = await _AsyncioSender.create(host, port)
            else:
                self._sender = await _AnyioSender.create(host, port)

        await self._sender.send(message)

    async def aclose(self) -> None:
        """Close the UDP socket, if one has been opened."""
        if self._sender is not None:
            sender, self._sender = self._sender, None
            await sender.aclose()
