"""Tests for the HTTP/3 protocol handler."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioquic.quic.events import QuicEvent, StopSendingReceived, StreamReset

from anycorn.config import Config
from anycorn.protocol.events import Body, EndBody, Event, Response, StreamClosed
from anycorn.protocol.h3 import H3Protocol
from anycorn.typing import ConnectionState, TLSExtension
from anycorn.worker_context import WorkerContext

if TYPE_CHECKING:
    from aioquic.h3.events import H3Event


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


class _RecordingStream:
    """Stands in for the HTTPStream/WSStream a request is being served by."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class _RecordingH3Connection:
    """The slice of aioquic's H3Connection that H3Protocol uses, keeping its rule.

    aioquic asserts "cannot call write() after reset()" once its peer has reset a
    stream. Scripting that as a mock side effect only ever asserts against the
    test's own idea of when it fires; enforcing the rule here means a send anycorn
    ought to have skipped raises exactly as the real thing would.

    Writes are recorded before the rule is applied, so a send that was skipped can
    be told apart from one that was attempted and blew up - which is the whole
    point of tracking reset streams, and something no assertion from outside the
    connection can distinguish.
    """

    def __init__(self, events: list[H3Event] | None = None) -> None:
        self.events = events if events is not None else []
        self.attempted: list[tuple[str, int]] = []
        self.written: list[tuple[str, int]] = []
        self.reset_by_peer: set[int] = set()

    def handle_event(self, event: QuicEvent) -> list[H3Event]:
        if isinstance(event, (StreamReset, StopSendingReceived)):
            self.reset_by_peer.add(event.stream_id)
        return self.events

    def _write(self, kind: str, stream_id: int) -> None:
        self.attempted.append((kind, stream_id))
        if stream_id in self.reset_by_peer:
            msg = "cannot call write() after reset()"
            raise AssertionError(msg)
        self.written.append((kind, stream_id))

    # Signatures mirror aioquic's, so what anycorn calls is what is checked here
    def send_headers(
        self,
        stream_id: int,
        headers: list[tuple[bytes, bytes]],  # noqa: ARG002
        end_stream: bool = False,  # noqa: ARG002, FBT001, FBT002
    ) -> None:
        self._write("headers", stream_id)

    def send_data(
        self,
        stream_id: int,
        data: bytes,  # noqa: ARG002
        end_stream: bool,  # noqa: ARG002, FBT001
    ) -> None:
        self._write("data", stream_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "quic_event",
    [
        StopSendingReceived(error_code=0, stream_id=1),
        StreamReset(error_code=0, stream_id=1),
    ],
)
async def test_peer_reset_closes_the_stream_and_ends_writes_to_it(
    quic_event: QuicEvent,
) -> None:
    """A peer STOP_SENDING/RESET_STREAM must tear the stream down, so the app stops.

    aioquic resets our sender on these, so nothing more can be sent; the stream is
    dropped and handed a StreamClosed so the app sees http.disconnect, and anything
    the app goes on to send is dropped rather than pushed at aioquic.

    The reset being remembered is asserted by that last part rather than by reading
    the bookkeeping, since being skipped is the only reason to remember it.

    https://github.com/pgjones/hypercorn/issues/352
    """
    protocol = _make_protocol()
    stream = _RecordingStream()
    protocol.streams = {1: stream}  # type: ignore[dict-item]
    connection = _RecordingH3Connection()
    protocol.connection = connection

    await protocol.handle(quic_event)

    assert 1 not in protocol.streams
    assert stream.events == [StreamClosed(stream_id=1)]

    # An app that has not noticed the disconnect yet, or responds regardless
    await protocol.stream_send(Response(stream_id=1, headers=[], status_code=200))
    await protocol.stream_send(Body(stream_id=1, data=b"late"))
    await protocol.stream_send(EndBody(stream_id=1))

    assert connection.attempted == []  # not merely survived: never attempted
    protocol.send.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_stream_send_survives_a_racing_reset_assertion() -> None:
    """A reset landing mid-send makes aioquic assert; that must not crash the send.

    If the reset is recorded only after the app's send has begun, the aioquic call
    asserts. stream_send swallows it and forgets the stream instead - and skips the
    sends that follow, so the assertion is hit once rather than on every write.

    https://github.com/pgjones/hypercorn/issues/352
    """
    protocol = _make_protocol()
    stream = _RecordingStream()
    protocol.streams = {1: stream}  # type: ignore[dict-item]
    # Reset by the peer without H3Protocol having been told, which is the race
    connection = _RecordingH3Connection()
    connection.reset_by_peer.add(1)
    protocol.connection = connection

    await protocol.stream_send(EndBody(stream_id=1))  # must not raise

    assert 1 not in protocol.streams
    assert stream.events == [StreamClosed(stream_id=1)]
    assert connection.attempted == [("data", 1)]
    protocol.send.assert_not_awaited()  # type: ignore[attr-defined]

    # Having found out the hard way, it does not go back for more
    await protocol.stream_send(Body(stream_id=1, data=b"later still"))
    assert connection.attempted == [("data", 1)]


@pytest.mark.anyio
async def test_closing_a_reset_stream_stops_it_being_remembered() -> None:
    """StreamClosed clears the reset, so the id is not skipped for ever.

    Nothing is skipped wrongly today, since QUIC does not reuse a stream id within
    a connection - but the set would otherwise grow for the life of the connection,
    holding an entry per cancelled request, and the discard that prevents that had
    nothing covering it.
    """
    protocol = _make_protocol()
    protocol.streams = {1: _RecordingStream()}  # type: ignore[dict-item]
    connection = _RecordingH3Connection()
    protocol.connection = connection

    await protocol.handle(StreamReset(error_code=0, stream_id=1))
    await protocol.stream_send(StreamClosed(stream_id=1))

    # No longer treated as reset, so a send on that id reaches the connection again
    connection.reset_by_peer.clear()
    await protocol.stream_send(Body(stream_id=1, data=b"reused"))

    assert connection.attempted == [("data", 1)]
