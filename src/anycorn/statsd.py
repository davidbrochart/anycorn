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
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.dogstatsd_tags = config.dogstatsd_tags
        self.prefix = config.statsd_prefix
        if len(self.prefix) and self.prefix[-1] != ".":
            self.prefix += "."

    async def critical(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().critical(message, *args, **kwargs)
        await self.increment("anycorn.log.critical", 1)

    async def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().error(message, *args, **kwargs)
        await self.increment("anycorn.log.error", 1)

    async def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().warning(message, *args, **kwargs)
        await self.increment("anycorn.log.warning", 1)

    async def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().info(message, *args, **kwargs)

    async def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().debug(message, *args, **kwargs)

    async def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        await super().exception(message, *args, **kwargs)
        await self.increment("anycorn.log.exception", 1)

    async def log(self, level: int, message: str, *args: Any, **kwargs: Any) -> None:
        try:
            extra = kwargs.get("extra", None)
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
        except Exception:
            await super().warning("Failed to log to statsd", exc_info=True)

    async def access(
        self, request: WWWScope, response: ResponseSummary, request_time: float
    ) -> None:
        await super().access(request, response, request_time)
        await self.histogram("anycorn.request.duration", request_time * 1_000)
        await self.increment("anycorn.requests", 1)
        await self.increment(f"anycorn.request.status.{response['status']}", 1)

    async def gauge(self, name: str, value: int) -> None:
        await self._send(f"{self.prefix}{name}:{value}|g")

    async def increment(self, name: str, value: int, sampling_rate: float = 1.0) -> None:
        await self._send(f"{self.prefix}{name}:{value}|c|@{sampling_rate}")

    async def decrement(self, name: str, value: int, sampling_rate: float = 1.0) -> None:
        await self._send(f"{self.prefix}{name}:-{value}|c|@{sampling_rate}")

    async def histogram(self, name: str, value: float) -> None:
        await self._send(f"{self.prefix}{name}:{value}|ms")

    async def _send(self, message: str) -> None:
        if self.dogstatsd_tags:
            message = f"{message}|#{self.dogstatsd_tags}"
        await self._socket_send(message.encode("ascii"))

    async def _socket_send(self, message: bytes) -> None:
        raise NotImplementedError()


class StatsdLogger(BaseStatsdLogger):
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
