"""Internal event types used for communication between server components."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


class Event(ABC):  # noqa: B024
    """Base class for all internal server events."""


@dataclass(frozen=True)
class RawData(Event):
    """Event carrying raw bytes received from the network."""

    data: bytes
    address: tuple[str, int] | None = None


@dataclass(frozen=True)
class Closed(Event):
    """Event signalling that a connection has been closed."""


@dataclass(frozen=True)
class Updated(Event):
    """Event signalling a protocol state change."""

    idle: bool
