"""Internal stream-level events used between protocol layers and stream handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anycorn.typing import ConnectionState

_MIN_INFORMATIONAL_STATUS = 100
_MAX_INFORMATIONAL_STATUS = 200


@dataclass(frozen=True)
class Event:
    """Base class for all stream events."""

    stream_id: int


@dataclass(frozen=True)
class Request(Event):
    """Represents an incoming HTTP request."""

    headers: list[tuple[bytes, bytes]]
    http_version: str
    method: str
    raw_path: bytes
    state: ConnectionState


@dataclass(frozen=True)
class Body(Event):
    """Represents a chunk of request body data."""

    data: bytes


@dataclass(frozen=True)
class EndBody(Event):
    """Signals the end of the request body."""


@dataclass(frozen=True)
class Trailers(Event):
    """Represents HTTP trailers sent after the body."""

    headers: list[tuple[bytes, bytes]]


@dataclass(frozen=True)
class Data(Event):
    """Represents raw data (e.g. WebSocket frames)."""

    data: bytes


@dataclass(frozen=True)
class EndData(Event):
    """Signals the end of a data stream (e.g. WebSocket close)."""


@dataclass(frozen=True)
class Response(Event):
    """Represents an HTTP response to be sent."""

    headers: list[tuple[bytes, bytes]]
    status_code: int


@dataclass(frozen=True)
class InformationalResponse(Event):
    """Represents a 1XX informational HTTP response."""

    headers: list[tuple[bytes, bytes]]
    status_code: int

    def __post_init__(self) -> None:
        """Validate that the status code is in the 1XX range."""
        if (
            self.status_code >= _MAX_INFORMATIONAL_STATUS
            or self.status_code < _MIN_INFORMATIONAL_STATUS
        ):
            msg = f"Status code must be 1XX not {self.status_code}"
            raise ValueError(msg)


@dataclass(frozen=True)
class StreamClosed(Event):
    """Signals that the stream has been closed."""
