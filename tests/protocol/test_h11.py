from __future__ import annotations

from typing import Any
from unittest.mock import Mock, call

import h11
import pytest
from _pytest.monkeypatch import MonkeyPatch

import anycorn.protocol.h11
from anycorn.config import Config
from anycorn.events import Closed, RawData, Updated
from anycorn.protocol.events import Body, Data, EndBody, EndData, Request, Response, StreamClosed
from anycorn.protocol.h11 import H2CProtocolRequiredError, H2ProtocolAssumedError, H11Protocol
from anycorn.protocol.http_stream import HTTPStream
from anycorn.typing import ConnectionState
from anycorn.typing import Event as IOEvent

# from anycorn.worker_context import EventWrapper

try:
    from unittest.mock import AsyncMock
except ImportError:
    # Python < 3.8
    from unittest.mock import AsyncMock


BASIC_HEADERS = [("Host", "anycorn"), ("Connection", "close")]


@pytest.fixture(name="protocol")
async def _protocol(monkeypatch: MonkeyPatch) -> H11Protocol:
    MockHTTPStream = Mock()
    MockHTTPStream.return_value = AsyncMock(spec=HTTPStream)
    monkeypatch.setattr(anycorn.protocol.h11, "HTTPStream", MockHTTPStream)
    context = Mock()
    context.event_class.return_value = AsyncMock(spec=IOEvent)
    context.mark_request = AsyncMock()
    context.terminate = context.event_class()
    context.terminated = context.event_class()
    context.terminated.is_set.return_value = False
    return H11Protocol(
        AsyncMock(),
        Config(),
        context,
        AsyncMock(),
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )


@pytest.mark.anyio
async def test_protocol_send_response(protocol: H11Protocol) -> None:
    await protocol.stream_send(Response(stream_id=1, status_code=201, headers=[]))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert (
        protocol.send.call_args_list  # type: ignore[attr-defined]
        == [
            call(
                RawData(
                    data=(
                        b"HTTP/1.1 201 \r\ndate: Thu, 01 Jan 1970 01:23:20 GMT\r\n"
                        b"server: anycorn-h11\r\nConnection: close\r\n\r\n"
                    )
                )
            )
        ]
    )


@pytest.mark.anyio
async def test_protocol_preserve_headers(protocol: H11Protocol) -> None:
    await protocol.stream_send(
        Response(stream_id=1, status_code=201, headers=[(b"X-Special", b"Value")])
    )
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert (
        protocol.send.call_args_list  # type: ignore[attr-defined]
        == [
            call(
                RawData(
                    data=(
                        b"HTTP/1.1 201 \r\nX-Special: Value\r\n"
                        b"date: Thu, 01 Jan 1970 01:23:20 GMT\r\n"
                        b"server: anycorn-h11\r\nConnection: close\r\n\r\n"
                    )
                )
            )
        ]
    )


@pytest.mark.anyio
async def test_protocol_send_data(protocol: H11Protocol) -> None:
    await protocol.stream_send(Data(stream_id=1, data=b"hello"))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert protocol.send.call_args_list == [call(RawData(data=b"hello"))]  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_protocol_send_body(protocol: H11Protocol) -> None:
    await protocol.handle(
        RawData(data=b"GET / HTTP/1.1\r\nHost: anycorn\r\nConnection: close\r\n\r\n")
    )
    await protocol.stream_send(
        Response(stream_id=1, status_code=200, headers=[(b"content-length", b"5")])
    )
    await protocol.stream_send(Body(stream_id=1, data=b"hello"))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert protocol.send.call_args_list == [  # type: ignore[attr-defined]
        call(Updated(idle=False)),
        call(
            RawData(
                data=b"HTTP/1.1 200 \r\ncontent-length: 5\r\ndate: Thu, 01 Jan 1970 01:23:20 GMT\r\nserver: anycorn-h11\r\nConnection: close\r\n\r\n"  # noqa: E501
            )
        ),
        call(RawData(data=b"hello")),
    ]


@pytest.mark.anyio
async def test_protocol_keep_alive_max_requests(protocol: H11Protocol) -> None:
    data = b"GET / HTTP/1.1\r\nHost: anycorn\r\n\r\n"
    protocol.config.keep_alive_max_requests = 0
    await protocol.handle(RawData(data=data))
    await protocol.stream_send(Response(stream_id=1, status_code=200, headers=[]))
    await protocol.stream_send(EndBody(stream_id=1))
    await protocol.stream_send(StreamClosed(stream_id=1))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert protocol.send.call_args_list[3] == call(Closed())  # type: ignore[attr-defined]


@pytest.mark.anyio
@pytest.mark.parametrize("keep_alive, expected", [(True, Updated(idle=True)), (False, Closed())])
async def test_protocol_send_stream_closed(
    keep_alive: bool, expected: Any, protocol: H11Protocol
) -> None:
    data = b"GET / HTTP/1.1\r\nHost: anycorn\r\n"
    if keep_alive:
        data += b"\r\n"
    else:
        data += b"Connection: close\r\n\r\n"
    await protocol.handle(RawData(data=data))
    await protocol.stream_send(Response(stream_id=1, status_code=200, headers=[]))
    await protocol.stream_send(EndBody(stream_id=1))
    await protocol.stream_send(StreamClosed(stream_id=1))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert protocol.send.call_args_list[3] == call(expected)  # type: ignore[attr-defined]


# FIXME
# @pytest.mark.asyncio
# async def test_protocol_instant_recycle(
#     protocol: H11Protocol, event_loop: asyncio.AbstractEventLoop
# ) -> None:
#     # This test task acts as the asgi app, spawned tasks act as the
#     # server.
#     data = b"GET / HTTP/1.1\r\nHost: anycorn\r\n\r\n"
#     # This test requires a real event as the handling should pause on
#     # the instant receipt
#     protocol.can_read = EventWrapper()
#     task = event_loop.create_task(protocol.handle(RawData(data=data)))
#     await asyncio.sleep(0)  # Switch to task
#     assert protocol.stream is not None
#     assert task.done()
#     await protocol.stream_send(Response(stream_id=1, status_code=200, headers=[]))
#     await protocol.stream_send(EndBody(stream_id=1))
#     task = event_loop.create_task(protocol.handle(RawData(data=data)))
#     await asyncio.sleep(0)  # Switch to task
#     await protocol.stream_send(StreamClosed(stream_id=1))
#     await asyncio.sleep(0)  # Switch to task
#     # Should have recycled, i.e. a stream should exist
#     assert protocol.stream is not None
#     assert task.done()


@pytest.mark.anyio
async def test_protocol_send_end_data(protocol: H11Protocol) -> None:
    protocol.stream = AsyncMock()
    await protocol.stream_send(EndData(stream_id=1))
    assert protocol.stream is not None


@pytest.mark.anyio
async def test_protocol_handle_closed(protocol: H11Protocol) -> None:
    await protocol.handle(
        RawData(data=b"GET / HTTP/1.1\r\nHost: anycorn\r\nConnection: close\r\n\r\n")
    )
    stream = protocol.stream
    await protocol.handle(Closed())
    stream.handle.assert_called()  # type: ignore[attr-defined]
    assert stream.handle.call_args_list == [  # type: ignore[attr-defined]
        call(
            Request(
                stream_id=1,
                headers=[(b"host", b"anycorn"), (b"connection", b"close")],
                http_version="1.1",
                method="GET",
                raw_path=b"/",
                state=ConnectionState({}),
            )
        ),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]


@pytest.mark.anyio
async def test_protocol_handle_request(protocol: H11Protocol) -> None:
    client = h11.Connection(h11.CLIENT)
    await protocol.handle(
        RawData(data=client.send(h11.Request(method="GET", target="/?a=b", headers=BASIC_HEADERS)))
    )
    protocol.stream.handle.assert_called()  # type: ignore[attr-defined]
    assert protocol.stream.handle.call_args_list == [  # type: ignore[attr-defined]
        call(
            Request(
                stream_id=1,
                headers=[(b"host", b"anycorn"), (b"connection", b"close")],
                http_version="1.1",
                method="GET",
                raw_path=b"/?a=b",
                state=ConnectionState({}),
            )
        ),
        call(EndBody(stream_id=1)),
    ]


@pytest.mark.anyio
async def test_protocol_handle_request_with_raw_headers(protocol: H11Protocol) -> None:
    protocol.config.h11_pass_raw_headers = True
    client = h11.Connection(h11.CLIENT)
    headers = BASIC_HEADERS + [("FOO_BAR", "foobar")]
    await protocol.handle(
        RawData(data=client.send(h11.Request(method="GET", target="/?a=b", headers=headers)))
    )
    protocol.stream.handle.assert_called()  # type: ignore[attr-defined]
    assert protocol.stream.handle.call_args_list == [  # type: ignore[attr-defined]
        call(
            Request(
                stream_id=1,
                headers=[
                    (b"Host", b"anycorn"),
                    (b"Connection", b"close"),
                    (b"FOO_BAR", b"foobar"),
                ],
                http_version="1.1",
                method="GET",
                raw_path=b"/?a=b",
                state=ConnectionState({}),
            )
        ),
        call(EndBody(stream_id=1)),
    ]


@pytest.mark.anyio
async def test_protocol_handle_protocol_error(protocol: H11Protocol) -> None:
    await protocol.handle(RawData(data=b"broken nonsense\r\n\r\n"))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert (
        protocol.send.call_args_list  # type: ignore[attr-defined]
        == [
            call(
                RawData(
                    data=b"HTTP/1.1 400 \r\ncontent-length: 0\r\nconnection: close\r\n"
                    b"date: Thu, 01 Jan 1970 01:23:20 GMT\r\nserver: anycorn-h11\r\n\r\n"
                )
            ),
            call(RawData(data=b"")),
            call(Closed()),
        ]
    )


@pytest.mark.anyio
async def test_protocol_handle_send_client_error(protocol: H11Protocol) -> None:
    client = h11.Connection(h11.CLIENT)
    await protocol.handle(
        RawData(data=client.send(h11.Request(method="GET", target="/?a=b", headers=BASIC_HEADERS)))
    )
    await protocol.handle(RawData(data=b"some body"))
    # This next line should not cause an error
    await protocol.stream_send(Response(stream_id=1, status_code=200, headers=[]))


@pytest.mark.anyio
async def test_protocol_handle_pipelining(protocol: H11Protocol) -> None:
    protocol.can_read.wait.side_effect = Exception()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        await protocol.handle(
            RawData(
                data=b"GET / HTTP/1.1\r\nHost: anycorn\r\nConnection: keep-alive\r\n\r\n"
                b"GET / HTTP/1.1\r\nHost: anycorn\r\nConnection: close\r\n\r\n"
            )
        )
    protocol.can_read.clear.assert_called()  # type: ignore[attr-defined]
    protocol.can_read.wait.assert_called()  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_protocol_handle_continue_request(protocol: H11Protocol) -> None:
    client = h11.Connection(h11.CLIENT)
    await protocol.handle(
        RawData(
            data=client.send(
                h11.Request(
                    method="POST",
                    target="/?a=b",
                    headers=BASIC_HEADERS
                    + [("transfer-encoding", "chunked"), ("expect", "100-continue")],
                )
            )
        )
    )
    assert protocol.send.call_args[0][0] == RawData(  # type: ignore[attr-defined]
        data=b"HTTP/1.1 100 \r\ndate: Thu, 01 Jan 1970 01:23:20 GMT\r\nserver: anycorn-h11\r\n\r\n"
    )


@pytest.mark.anyio
async def test_protocol_handle_max_incomplete(monkeypatch: MonkeyPatch) -> None:
    config = Config()
    config.h11_max_incomplete_size = 5
    MockHTTPStream = AsyncMock()
    MockHTTPStream.return_value = AsyncMock(spec=HTTPStream)
    monkeypatch.setattr(anycorn.protocol.h11, "HTTPStream", MockHTTPStream)
    context = Mock()
    context.event_class.return_value = AsyncMock(spec=IOEvent)
    protocol = H11Protocol(
        AsyncMock(),
        config,
        context,
        AsyncMock(),
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    await protocol.handle(RawData(data=b"GET / HTTP/1.1\r\nHost: anycorn\r\n"))
    protocol.send.assert_called()  # type: ignore[attr-defined]
    assert (
        protocol.send.call_args_list  # type: ignore[attr-defined]
        == [
            call(
                RawData(
                    data=b"HTTP/1.1 431 \r\ncontent-length: 0\r\nconnection: close\r\n"
                    b"date: Thu, 01 Jan 1970 01:23:20 GMT\r\nserver: anycorn-h11\r\n\r\n"
                )
            ),
            call(RawData(data=b"")),
            call(Closed()),
        ]
    )


@pytest.mark.anyio
async def test_protocol_handle_h2c_upgrade(protocol: H11Protocol) -> None:
    with pytest.raises(H2CProtocolRequiredError) as exc_info:
        await protocol.handle(
            RawData(
                data=(
                    b"GET / HTTP/1.1\r\nHost: anycorn\r\n"
                    b"upgrade: h2c\r\nhttp2-settings: abcd\r\n\r\nbbb"
                )
            )
        )
    assert (
        protocol.send.call_args_list  # type: ignore[attr-defined]
        == [
            call(Updated(idle=False)),
            call(
                RawData(
                    b"HTTP/1.1 101 \r\n"
                    b"date: Thu, 01 Jan 1970 01:23:20 GMT\r\n"
                    b"server: anycorn-h11\r\n"
                    b"connection: upgrade\r\n"
                    b"upgrade: h2c\r\n"
                    b"\r\n"
                )
            ),
        ]
    )
    assert exc_info.value.data == b"bbb"
    assert exc_info.value.headers == [
        (b":method", b"GET"),
        (b":path", b"/"),
        (b":authority", b"anycorn"),
        (b"host", b"anycorn"),
        (b"upgrade", b"h2c"),
        (b"http2-settings", b"abcd"),
    ]
    assert exc_info.value.settings == "abcd"


@pytest.mark.anyio
async def test_protocol_handle_h2_prior(protocol: H11Protocol) -> None:
    with pytest.raises(H2ProtocolAssumedError) as exc_info:
        await protocol.handle(RawData(data=b"PRI * HTTP/2.0\r\n\r\nbbb"))

    assert exc_info.value.data == b"PRI * HTTP/2.0\r\n\r\nbbb"


@pytest.mark.anyio
async def test_protocol_handle_data_post_response(protocol: H11Protocol) -> None:
    await protocol.handle(
        RawData(data=b"POST / HTTP/1.1\r\nHost: anycorn\r\nContent-Length: 4\r\n\r\n")
    )
    await protocol.stream_send(Response(stream_id=1, status_code=201, headers=[]))
    await protocol.stream_send(EndBody(stream_id=1))
    await protocol.handle(RawData(data=b"abcd"))


@pytest.mark.anyio
async def test_protocol_handle_data_post_end(protocol: H11Protocol) -> None:
    await protocol.handle(
        RawData(data=b"POST / HTTP/1.1\r\nHost: anycorn\r\nContent-Length: 10\r\n\r\n")
    )
    await protocol.stream_send(Response(stream_id=1, status_code=201, headers=[]))
    await protocol.stream_send(EndBody(stream_id=1))
    # Key is that this doesn't error
    await protocol.handle(RawData(data=b"abcdefghij"))


@pytest.mark.anyio
async def test_protocol_handle_data_post_close(protocol: H11Protocol) -> None:
    await protocol.handle(
        RawData(data=b"POST / HTTP/1.1\r\nHost: anycorn\r\nContent-Length: 10\r\n\r\n")
    )
    await protocol.stream_send(StreamClosed(stream_id=1))
    assert protocol.stream is None
    # Key is that this doesn't error
    await protocol.handle(RawData(data=b"abcdefghij"))
