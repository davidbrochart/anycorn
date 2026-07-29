"""Tests for the HTTP/2 protocol handler."""

from __future__ import annotations

from unittest.mock import Mock, call

import anyio
import pytest
from h2.connection import H2Connection
from h2.events import ConnectionTerminated, PushedStreamReceived

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.events import Closed, Event, RawData
from anycorn.protocol.events import Trailers
from anycorn.protocol.h2 import (
    BUFFER_HIGH_WATER,
    BufferCompleteError,
    H2Protocol,
    StreamBuffer,
)
from anycorn.task_group import TaskGroup
from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, ConnectionState, Scope
from anycorn.worker_context import EventWrapper, WorkerContext

try:
    from unittest.mock import AsyncMock
except ImportError:
    # Python < 3.8
    from unittest.mock import AsyncMock


@pytest.mark.anyio
async def test_stream_buffer_push_and_pop() -> None:
    """The writer resumes once the buffer has drained, not once a chunk is taken.

    Resuming on the size of the chunk popped meant a zero-length pop - which is
    what _send_data does once the peer's flow control window is exhausted - let
    the writer on with the buffer still above the high water mark.
    """
    stream_buffer = StreamBuffer(EventWrapper)
    pushed = False

    async def _push_over_limit() -> None:
        nonlocal pushed
        await stream_buffer.push(b"a" * (BUFFER_HIGH_WATER + 1))
        pushed = True

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_push_over_limit)
        await anyio.wait_all_tasks_blocked()
        assert not pushed  # Blocked as over high water

        # Nothing could be sent, so nothing has drained and the writer stays put
        await stream_buffer.pop(0)
        await anyio.wait_all_tasks_blocked()
        assert not pushed

        # Drained some, but not to below the low water mark
        await stream_buffer.pop(BUFFER_HIGH_WATER // 4)
        await anyio.wait_all_tasks_blocked()
        assert not pushed

        # Now under it, so there is room for the writer to go on
        await stream_buffer.pop(BUFFER_HIGH_WATER)
        await anyio.wait_all_tasks_blocked()
        assert pushed


@pytest.mark.anyio
async def test_stream_buffer_drain() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.push(b"a" * 10)
    drained = False

    async def _drain() -> None:
        nonlocal drained
        await stream_buffer.drain()
        drained = True

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_drain)
        await anyio.wait_all_tasks_blocked()
        assert not drained  # Blocked, as the buffer is not empty

        await stream_buffer.pop(20)
        await anyio.wait_all_tasks_blocked()
        assert drained


@pytest.mark.anyio
async def test_stream_buffer_closed() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.close()
    await stream_buffer._is_empty.wait()
    await stream_buffer._paused.wait()
    assert True
    with pytest.raises(BufferCompleteError):
        await stream_buffer.push(b"a")


@pytest.mark.anyio
async def test_stream_buffer_complete() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.push(b"a" * 10)
    assert not stream_buffer.complete
    stream_buffer.set_complete()
    assert not stream_buffer.complete
    await stream_buffer.pop(20)
    assert stream_buffer.complete


@pytest.mark.anyio
async def test_protocol_handle_protocol_error() -> None:
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        AsyncMock(),
        ConnectionState({}),
        None,
        None,
        AsyncMock(),
        None,
    )
    await protocol.handle(RawData(data=b"broken nonsense\r\n\r\n"))
    protocol.send.assert_awaited()  # type: ignore[attr-defined]
    assert protocol.send.call_args_list == [call(Closed())]  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_protocol_keep_alive_max_requests() -> None:
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        AsyncMock(),
        ConnectionState({}),
        None,
        None,
        AsyncMock(),
        None,
    )
    protocol.config.keep_alive_max_requests = 0
    client = H2Connection()
    client.initiate_connection()
    headers = [
        (":method", "GET"),
        (":path", "/reqinfo"),
        (":authority", "anycorn"),
        (":scheme", "https"),
    ]
    client.send_headers(1, headers, end_stream=True)
    await protocol.handle(RawData(data=client.data_to_send()))
    protocol.send.assert_awaited()  # type: ignore[attr-defined]
    events = client.receive_data(protocol.send.call_args_list[1].args[0].data)  # type: ignore[attr-defined]
    assert isinstance(events[-1], ConnectionTerminated)


@pytest.mark.anyio
async def test_stream_send_trailers_ends_stream() -> None:
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        AsyncMock(),
        ConnectionState({}),
        None,
        None,
        AsyncMock(),
        None,
    )
    protocol.connection.send_headers = Mock()  # type: ignore[method-assign]
    protocol.priority.insert_stream(1)
    protocol.stream_buffers[1] = StreamBuffer(EventWrapper)
    protocol.stream_buffers[1].set_complete()
    await protocol.stream_buffers[1]._is_empty.set()

    with anyio.fail_after(2):
        await protocol.stream_send(Trailers(stream_id=1, headers=[(b"x", b"y")]))

    protocol.connection.send_headers.assert_called_once_with(1, [(b"x", b"y")], end_stream=True)


async def _push_once_app(
    scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    """Respond to any request; on "/" also push one resource first."""
    assert scope["type"] == "http"
    while True:
        event = await receive()
        if not event.get("more_body", False):
            break
    if scope["path"] == "/":
        await send({"type": "http.response.push", "path": "/pushed", "headers": []})
    body = scope["path"].encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"%d" % len(body))],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


@pytest.mark.anyio
async def test_server_push_counts_once_towards_keep_alive() -> None:
    """A pushed stream must count once toward keep_alive_requests, like a request.

    _create_server_push builds the pushed stream through _create_stream, which already
    increments keep_alive_requests; a second increment counted every push twice, so a
    connection using server push hit keep_alive_max_requests and closed too early.

    Driven through a real task group and a real ASGI app that emits an
    http.response.push, so the count reflects what actually flows, not a stubbed path.
    """
    sent = bytearray()

    async def send(event: Event) -> None:
        if isinstance(event, RawData):
            sent.extend(event.data)

    async with TaskGroup() as task_group:
        protocol = H2Protocol(
            ASGIWrapper(_push_once_app),
            Config(),
            WorkerContext(None),
            task_group,
            ConnectionState({}),
            ("127.0.0.1", 80),
            ("127.0.0.1", 8000),
            send,
            None,
        )
        await protocol.initiate()

        client = H2Connection()
        client.initiate_connection()
        client.send_headers(
            1,
            [
                (b":method", b"GET"),
                (b":path", b"/"),
                (b":authority", b"anycorn"),
                (b":scheme", b"https"),
            ],
            end_stream=True,
        )
        await protocol.handle(RawData(data=client.data_to_send()))
        # Let the app run: receive the request, emit the push, and respond.
        await anyio.wait_all_tasks_blocked()

        # The push promise really went out (the else-branch ran, not the refusal)...
        events = client.receive_data(bytes(sent))
        assert any(isinstance(event, PushedStreamReceived) for event in events)
        # ... and it added exactly one: request (1) + pushed stream (1), not 3.
        assert protocol.keep_alive_requests == 2  # noqa: PLR2004

        await protocol.handle(Closed())  # stop the send task so the group can exit


@pytest.mark.anyio
async def test_reset_stream_does_not_leak_send_state() -> None:
    """A reset stream must not leave its send buffer and priority entry behind.

    Only _send_data tidies those up, and only once a response has finished. A
    stream the peer resets never gets there, so on a long-lived connection - a
    browser cancelling requests, say - both grew without bound.
    """
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        AsyncMock(),
        ConnectionState({}),
        None,
        None,
        AsyncMock(),
        None,
    )
    client = H2Connection()
    client.initiate_connection()
    client.send_headers(
        1,
        [
            (b":method", b"POST"),
            (b":path", b"/"),
            (b":authority", b"anycorn"),
            (b":scheme", b"https"),
        ],
        end_stream=False,
    )
    await protocol.handle(RawData(data=client.data_to_send()))
    assert 1 in protocol.stream_buffers
    assert 1 in protocol.priority._streams

    client.reset_stream(1)
    await protocol.handle(RawData(data=client.data_to_send()))

    assert protocol.streams == {}
    assert protocol.stream_buffers == {}
    assert 1 not in protocol.priority._streams
