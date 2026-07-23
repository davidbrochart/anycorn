"""Tests for the HTTP/3 protocol handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioquic.quic.events import QuicEvent, StopSendingReceived, StreamReset

from anycorn.config import Config
from anycorn.protocol.events import Body, EndBody, StreamClosed
from anycorn.protocol.h3 import H3Protocol
from anycorn.typing import ConnectionState, TLSExtension
from anycorn.worker_context import WorkerContext


def _make_protocol() -> H3Protocol:
    """Build a fully initialised H3Protocol over mock collaborators.

    Goes through the real constructor, so streams, _reset_streams and the rest are
    set up as in production; the quic connection is a mock, and tests that drive the
    H3 connection stub protocol.connection on top of it.
    """
    return H3Protocol(
        MagicMock(),  # app
        Config(),
        WorkerContext(None),
        MagicMock(),  # task_group
        ConnectionState({}),
        TLSExtension(),
        None,  # client
        None,  # server
        MagicMock(),  # quic
        AsyncMock(),  # send
    )


@pytest.mark.anyio
async def test_stream_send_stream_closed_removes_stream() -> None:
    protocol = _make_protocol()
    protocol.streams = {1: object(), 2: object()}  # type: ignore[dict-item]

    await protocol.stream_send(StreamClosed(stream_id=1))

    assert protocol.streams == {2: protocol.streams[2]}


@pytest.mark.anyio
async def test_stream_send_stream_closed_is_idempotent() -> None:
    protocol = _make_protocol()

    await protocol.stream_send(StreamClosed(stream_id=1))

    assert protocol.streams == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "quic_event",
    [
        StopSendingReceived(error_code=0, stream_id=1),
        StreamReset(error_code=0, stream_id=1),
    ],
)
async def test_peer_reset_closes_the_stream(quic_event: QuicEvent) -> None:
    """A peer STOP_SENDING/RESET_STREAM must tear the stream down, so the app stops.

    aioquic resets our sender on these, so nothing more can be sent; the stream is
    dropped and handed a StreamClosed so the app sees http.disconnect.

    https://github.com/pgjones/hypercorn/issues/352
    """
    protocol = _make_protocol()
    stream = MagicMock()
    stream.handle = AsyncMock()
    protocol.streams = {1: stream}
    protocol.connection = MagicMock()
    protocol.connection.handle_event = MagicMock(return_value=[])

    await protocol.handle(quic_event)

    assert 1 not in protocol.streams
    assert 1 in protocol._reset_streams
    stream.handle.assert_awaited_once_with(StreamClosed(stream_id=1))


@pytest.mark.anyio
async def test_stream_send_skips_a_reset_stream() -> None:
    """Once a stream is reset, sending on it is skipped, not pushed into aioquic.

    Sending would hit aioquic's "cannot call write() after reset()" assertion.
    """
    protocol = _make_protocol()
    protocol._reset_streams = {1}
    protocol.connection = MagicMock()

    await protocol.stream_send(Body(stream_id=1, data=b"late"))
    await protocol.stream_send(EndBody(stream_id=1))

    protocol.connection.send_data.assert_not_called()
    protocol.send.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_stream_send_survives_a_racing_reset_assertion() -> None:
    """A reset landing mid-send makes aioquic assert; that must not crash the send.

    If the reset is recorded only after the app's send has begun, the aioquic call
    asserts. stream_send swallows it and forgets the stream instead.

    https://github.com/pgjones/hypercorn/issues/352
    """
    protocol = _make_protocol()
    stream = MagicMock()
    stream.handle = AsyncMock()
    protocol.streams = {1: stream}
    protocol.connection = MagicMock()
    protocol.connection.send_data.side_effect = AssertionError("cannot call write() after reset()")

    await protocol.stream_send(EndBody(stream_id=1))  # must not raise

    assert 1 in protocol._reset_streams
    assert 1 not in protocol.streams
    protocol.send.assert_not_awaited()  # type: ignore[attr-defined]
