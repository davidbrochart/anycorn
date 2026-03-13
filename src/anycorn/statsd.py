"""StatsD logger integration for Anycorn."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

import anyio

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


class StatsdLogger(BaseStatsdLogger):
    """StatsD logger that sends metrics over UDP."""

    socket: anyio.abc.ConnectedUDPSocket | None

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.address = tuple(config.statsd_host.rsplit(":", 1))
        self.socket = None

    async def _socket_send(self, message: bytes) -> None:
        if self.socket is None:
            self.socket = await anyio.create_connected_udp_socket(
                self.address[0], int(self.address[1]), family=socket.AddressFamily.AF_INET
            )
        await self.socket.send(message)
