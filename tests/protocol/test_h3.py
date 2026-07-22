"""Tests for the HTTP/3 protocol handler."""

from __future__ import annotations

import pytest

from anycorn.protocol.events import StreamClosed
from anycorn.protocol.h3 import H3Protocol


@pytest.mark.anyio
async def test_stream_send_stream_closed_removes_stream() -> None:
    protocol = H3Protocol.__new__(H3Protocol)
    protocol.streams = {1: object(), 2: object()}

    await protocol.stream_send(StreamClosed(stream_id=1))

    assert protocol.streams == {2: protocol.streams[2]}


@pytest.mark.anyio
async def test_stream_send_stream_closed_is_idempotent() -> None:
    protocol = H3Protocol.__new__(H3Protocol)
    protocol.streams = {}

    await protocol.stream_send(StreamClosed(stream_id=1))

    assert protocol.streams == {}
