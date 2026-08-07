"""Tests for HTTP stream implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import call

import anyio
import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.protocol.events import (
    Body,
    EndBody,
    Event,
    InformationalResponse,
    Request,
    Response,
    StreamClosed,
    Trailers,
)
from anycorn.protocol.http_stream import ASGIHTTPState, HTTPStream
from anycorn.statsd import StatsdLogger
from anycorn.task_group import TaskGroup
from anycorn.typing import (
    ASGIReceiveCallable,
    ASGISendCallable,
    ConnectionState,
    HTTPResponseBodyEvent,
    HTTPResponsePathSendEvent,
    HTTPResponseStartEvent,
    HTTPScope,
    Scope,
)
from anycorn.utils import ClientDisconnected, UnexpectedMessageError, default_tls_extension
from anycorn.worker_context import WorkerContext
from tests.helpers import LogCapture, capture_logs

if TYPE_CHECKING:
    from pathlib import Path

try:
    from unittest.mock import AsyncMock
except ImportError:
    # Python < 3.8
    from unittest.mock import AsyncMock


@pytest.fixture(name="config")
def _config() -> Config:
    return Config()


@pytest.fixture(name="logs")
def _logs(config: Config) -> LogCapture:
    """Run the real access log against *config*, and collect what it writes."""
    return capture_logs(config)


@pytest.fixture(name="stream")
async def _stream(config: Config, logs: LogCapture) -> HTTPStream:  # noqa: ARG001
    stream = HTTPStream(
        AsyncMock(),
        config,
        WorkerContext(None),
        AsyncMock(),
        None,
        None,
        AsyncMock(),
        1,
        None,
    )
    stream.app_put = AsyncMock()
    return stream


@pytest.mark.parametrize("http_version", ["1.0", "1.1"])
@pytest.mark.anyio
async def test_handle_request_http_1(stream: HTTPStream, http_version: str) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version=http_version,
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    stream.task_group.spawn_app.assert_called()  # type: ignore[attr-defined]
    scope = stream.task_group.spawn_app.call_args[0][2]  # type: ignore[attr-defined]
    assert scope == {
        "type": "http",
        "http_version": http_version,
        "asgi": {"spec_version": "2.5", "version": "3.0"},
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"a=b",
        "root_path": stream.config.root_path,
        "headers": [],
        "client": None,
        "server": None,
        "extensions": {"http.response.pathsend": {}},
        "state": ConnectionState({}),
    }


@pytest.mark.anyio
async def test_handle_request_http_2(stream: HTTPStream) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    stream.task_group.spawn_app.assert_called()  # type: ignore[attr-defined]
    scope = stream.task_group.spawn_app.call_args[0][2]  # type: ignore[attr-defined]
    assert scope == {
        "type": "http",
        "http_version": "2",
        "asgi": {"spec_version": "2.5", "version": "3.0"},
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"a=b",
        "root_path": stream.config.root_path,
        "headers": [],
        "client": None,
        "server": None,
        "extensions": {
            "http.response.trailers": {},
            "http.response.early_hint": {},
            "http.response.push": {},
            "http.response.pathsend": {},
        },
        "state": ConnectionState({}),
    }


@pytest.mark.anyio
async def test_handle_request_http_tls() -> None:
    stream = HTTPStream(
        AsyncMock(),
        Config(),
        WorkerContext(None),
        AsyncMock(),
        None,
        None,
        AsyncMock(),
        1,
        default_tls_extension(),
    )
    stream.app_put = AsyncMock()
    capture_logs(stream.config)
    await stream.handle(
        Request(
            stream_id=1,
            http_version="1.1",
            headers=[],
            raw_path=b"/",
            method="GET",
            state=ConnectionState({}),
        )
    )
    scope = stream.task_group.spawn_app.call_args[0][2]  # type: ignore[attr-defined]
    assert "tls" in scope["extensions"]
    assert scope["extensions"]["tls"]["client_cert_chain"] == ()
    assert scope["scheme"] == "https"


@pytest.mark.anyio
async def test_handle_body(stream: HTTPStream) -> None:
    await stream.handle(Body(stream_id=1, data=b"data"))
    stream.app_put.assert_called()  # type: ignore[attr-defined]
    assert stream.app_put.call_args_list == [  # type: ignore[attr-defined]
        call({"type": "http.request", "body": b"data", "more_body": True})
    ]


@pytest.mark.anyio
async def test_handle_end_body(stream: HTTPStream) -> None:
    stream.app_put = AsyncMock()
    await stream.handle(EndBody(stream_id=1))
    stream.app_put.assert_called()
    assert stream.app_put.call_args_list == [
        call({"type": "http.request", "body": b"", "more_body": False})
    ]


@pytest.mark.anyio
async def test_handle_closed(stream: HTTPStream) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.handle(StreamClosed(stream_id=1))
    stream.app_put.assert_called()  # type: ignore[attr-defined]
    assert stream.app_put.call_args_list == [call({"type": "http.disconnect"})]  # type: ignore[attr-defined]


def _get_request() -> Request:
    return Request(
        stream_id=1,
        http_version="1.1",
        headers=[(b"host", b"anycorn")],
        raw_path=b"/",
        method="GET",
        state=ConnectionState({}),
    )


@pytest.mark.anyio
async def test_send_after_client_disconnect_raises(config: Config) -> None:
    """A real app that sends after the client has gone gets ClientDisconnected (spec 2.4).

    Driven through a real task group and a real ASGI app rather than poking app_send:
    the app receives the http.disconnect, then its next send() must raise.
    """
    outcome: dict[str, object] = {}

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        assert scope["type"] == "http"
        assert (await receive())["type"] == "http.disconnect"
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        except ClientDisconnected as exc:
            outcome["raised"] = isinstance(exc, OSError)

    async def send(event: Event) -> None:
        pass

    async with TaskGroup() as task_group:
        stream = HTTPStream(
            ASGIWrapper(app),
            config,
            WorkerContext(None),
            task_group,
            ("127.0.0.1", 1234),
            ("127.0.0.1", 8000),
            send,
            1,
            None,
        )
        await stream.handle(_get_request())
        await anyio.wait_all_tasks_blocked()
        await stream.handle(StreamClosed(stream_id=1))
        await anyio.wait_all_tasks_blocked()

    # An OSError subclass, as the spec requires, and the app really caught it.
    assert outcome == {"raised": True}


@pytest.mark.anyio
async def test_client_disconnect_left_uncaught_is_not_a_framework_error(
    config: Config, logs: LogCapture
) -> None:
    """The spec lets an app re-raise the disconnect; that clean exit is not logged as an error."""

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        assert scope["type"] == "http"
        assert (await receive())["type"] == "http.disconnect"
        # Do not catch: let ClientDisconnected propagate out of the app.
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(event: Event) -> None:
        pass

    async with TaskGroup() as task_group:
        stream = HTTPStream(
            ASGIWrapper(app),
            config,
            WorkerContext(None),
            task_group,
            ("127.0.0.1", 1234),
            ("127.0.0.1", 8000),
            send,
            1,
            None,
        )
        await stream.handle(_get_request())
        await anyio.wait_all_tasks_blocked()
        await stream.handle(StreamClosed(stream_id=1))
        await anyio.wait_all_tasks_blocked()

    assert logs.error == []


@pytest.mark.anyio
async def test_pathsend_extension_is_advertised(stream: HTTPStream) -> None:
    """Path send is protocol-agnostic, so it is offered on HTTP/1.1 too."""
    await stream.handle(_get_request())
    scope = stream.task_group.spawn_app.call_args[0][2]  # type: ignore[attr-defined]
    assert scope["extensions"]["http.response.pathsend"] == {}


@pytest.mark.anyio
async def test_pathsend_streams_the_named_file(stream: HTTPStream, tmp_path: Path) -> None:
    """A pathsend message streams the file at that path as the response body, then closes."""
    sent: list[Event] = []

    async def send(event: Event) -> None:
        sent.append(event)

    stream.send = send  # a real collector rather than the fixture's mock
    await stream.handle(_get_request())

    payload = b"the quick brown fox\n" * 5000  # larger than PATHSEND_CHUNK_SIZE
    file_path = tmp_path / "payload.bin"
    file_path.write_bytes(payload)

    await stream.app_send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(payload)).encode())],
        }
    )
    pathsend: HTTPResponsePathSendEvent = {
        "type": "http.response.pathsend",
        "path": str(file_path),
    }
    await stream.app_send(pathsend)

    # The whole file went out as body, and the response was finished off.
    body = b"".join(event.data for event in sent if isinstance(event, Body))
    assert body == payload
    assert any(isinstance(event, EndBody) for event in sent)
    assert any(isinstance(event, StreamClosed) for event in sent)


@pytest.mark.anyio
async def test_lowering_the_spec_version_advertises_it_and_drops_the_raise() -> None:
    """A configured spec_version below 2.4 is advertised, and send() no longer raises."""
    config = Config()
    config.asgi_spec_version = "2.3"
    outcome: dict[str, object] = {}

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        assert scope["asgi"]["spec_version"] == "2.3"
        assert (await receive())["type"] == "http.disconnect"
        # Below 2.4 the send after disconnect is dropped rather than raising, so the
        # handler runs to completion instead of being interrupted.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        outcome["completed"] = True

    async def send(event: Event) -> None:
        pass

    async with TaskGroup() as task_group:
        stream = HTTPStream(
            ASGIWrapper(app),
            config,
            WorkerContext(None),
            task_group,
            ("127.0.0.1", 1234),
            ("127.0.0.1", 8000),
            send,
            1,
            None,
        )
        await stream.handle(_get_request())
        await anyio.wait_all_tasks_blocked()
        await stream.handle(StreamClosed(stream_id=1))
        await anyio.wait_all_tasks_blocked()

    assert outcome == {"completed": True}


@pytest.mark.anyio
async def test_send_response(stream: HTTPStream, logs: LogCapture) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.app_send(
        cast(
            "HTTPResponseStartEvent",
            {"type": "http.response.start", "status": 200, "headers": []},
        )
    )
    assert stream.state == ASGIHTTPState.RESPONSE
    await stream.app_send(
        cast("HTTPResponseBodyEvent", {"type": "http.response.body", "body": b"Body"})
    )
    assert stream.state == ASGIHTTPState.CLOSED
    stream.send.assert_called()  # type: ignore[unresolved-attribute]
    assert stream.send.call_args_list == [  # type: ignore[unresolved-attribute]
        call(Response(stream_id=1, headers=[], status_code=200)),
        call(Body(stream_id=1, data=b"Body")),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]
    # The real access log ran, rather than a mock recording that it was called
    assert len(logs.access) == 1
    assert '"GET / 2" 200' in logs.access[0]


@pytest.mark.anyio
async def test_send_closed_does_not_double_log_on_concurrent_stream_close(
    stream: HTTPStream, logs: LogCapture
) -> None:
    """A StreamClosed racing the response's completion must not log the request twice.

    When the client closes just as the response finalises, the reader task can handle
    StreamClosed while EndBody is still in flight. Unless the stream is already CLOSED
    by then, that path logs the request (response=None) and _send_closed goes on to log
    it again with the full response. Marking CLOSED before the EndBody send closes the
    window; here the race is forced deterministically by handling StreamClosed from
    inside the EndBody send itself.

    https://github.com/pgjones/hypercorn/issues/357
    """
    await stream.handle(
        Request(
            stream_id=1,
            http_version="1.1",
            headers=[],
            raw_path=b"/",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.app_send(
        cast(
            "HTTPResponseStartEvent",
            {"type": "http.response.start", "status": 200, "headers": []},
        )
    )

    async def _close_during_end_body(event: Event) -> None:
        # The reader task running mid-send, exactly as the race schedules it.
        if isinstance(event, EndBody):
            await stream.handle(StreamClosed(stream_id=1))

    stream.send = AsyncMock(side_effect=_close_during_end_body)

    await stream.app_send(
        cast("HTTPResponseBodyEvent", {"type": "http.response.body", "body": b"Body"})
    )

    assert len(logs.access) == 1


@pytest.mark.anyio
async def test_invalid_server_name(stream: HTTPStream) -> None:
    stream.config.server_names = ["anycorn"]
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[(b"host", b"example.com")],
            raw_path=b"/",
            method="GET",
            state=ConnectionState({}),
        )
    )
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(
            Response(
                stream_id=1,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
                status_code=404,
            )
        ),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]
    # This shouldn't error
    await stream.handle(Body(stream_id=1, data=b"Body"))


@pytest.mark.anyio
async def test_send_push(stream: HTTPStream, http_scope: HTTPScope) -> None:
    stream.scope = http_scope
    stream.stream_id = 1
    await stream.app_send({"type": "http.response.push", "path": "/push", "headers": []})
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(
            Request(
                stream_id=1,
                headers=[(b":scheme", b"https")],
                http_version="2",
                method="GET",
                raw_path=b"/push",
                state=ConnectionState({}),
            )
        )
    ]


@pytest.mark.anyio
async def test_send_early_hint(stream: HTTPStream, http_scope: HTTPScope) -> None:
    stream.scope = http_scope
    stream.stream_id = 1
    await stream.app_send(
        {"type": "http.response.early_hint", "links": [b'</style.css>; rel="preload"; as="style"']}
    )
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(
            InformationalResponse(
                stream_id=1,
                headers=[(b"link", b'</style.css>; rel="preload"; as="style"')],
                status_code=103,
            )
        )
    ]


@pytest.mark.anyio
async def test_send_trailers(stream: HTTPStream) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[(b"te", b"trailers")],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.app_send(
        cast(
            "HTTPResponseStartEvent",
            {"type": "http.response.start", "status": 200, "trailers": True},
        )
    )
    await stream.app_send(
        cast("HTTPResponseBodyEvent", {"type": "http.response.body", "body": b"Body"})
    )
    await stream.app_send({"type": "http.response.trailers", "headers": [(b"X", b"V")]})
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(Response(stream_id=1, headers=[], status_code=200)),
        call(Body(stream_id=1, data=b"Body")),
        call(Trailers(stream_id=1, headers=[(b"X", b"V")])),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]


@pytest.mark.anyio
async def test_send_trailers_ignored(stream: HTTPStream) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],  # no TE: trailers header
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.app_send(
        cast(
            "HTTPResponseStartEvent",
            {"type": "http.response.start", "status": 200, "trailers": True},
        )
    )
    await stream.app_send(
        cast("HTTPResponseBodyEvent", {"type": "http.response.body", "body": b"Body"})
    )
    await stream.app_send({"type": "http.response.trailers", "headers": [(b"X", b"V")]})
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(Response(stream_id=1, headers=[], status_code=200)),
        call(Body(stream_id=1, data=b"Body")),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]


@pytest.mark.anyio
async def test_send_app_error(stream: HTTPStream, logs: LogCapture) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    await stream.app_send(None)
    stream.send.assert_called()  # type: ignore[attr-defined]
    assert stream.send.call_args_list == [  # type: ignore[attr-defined]
        call(
            Response(
                stream_id=1,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
                status_code=500,
            )
        ),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]
    assert len(logs.access) == 1
    assert '"GET / 2" 500' in logs.access[0]


@pytest.mark.parametrize(
    ("state", "message_type"),
    [
        (ASGIHTTPState.REQUEST, "not_a_real_type"),
        (ASGIHTTPState.RESPONSE, "http.response.start"),
        (ASGIHTTPState.TRAILERS, "http.response.start"),
        (ASGIHTTPState.CLOSED, "http.response.start"),
        (ASGIHTTPState.CLOSED, "http.response.body"),
        (ASGIHTTPState.CLOSED, "http.response.trailers"),
    ],
)
@pytest.mark.anyio
async def test_send_invalid_message_given_state(
    stream: HTTPStream, state: ASGIHTTPState, http_scope: HTTPScope, message_type: str
) -> None:
    stream.state = state
    stream.scope = http_scope
    with pytest.raises(UnexpectedMessageError):
        await stream.app_send({"type": message_type})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        ("201 NO CONTENT", [], b""),  # Status should be int
        (200, [("X-Foo", "foo")], b""),  # Headers should be bytes
        (200, [], "Body"),  # Body should be bytes
    ],
)
@pytest.mark.anyio
async def test_send_invalid_message(
    stream: HTTPStream,
    http_scope: HTTPScope,
    status: Any,  # noqa: ANN401
    headers: Any,  # noqa: ANN401
    body: Any,  # noqa: ANN401
) -> None:
    stream.scope = http_scope
    stream.state = ASGIHTTPState.REQUEST
    with pytest.raises((TypeError, ValueError)):  # noqa: PT012
        await stream.app_send(
            cast(
                "HTTPResponseStartEvent",
                {"type": "http.response.start", "headers": headers, "status": status},
            )
        )
        await stream.app_send(
            cast("HTTPResponseBodyEvent", {"type": "http.response.body", "body": body})
        )


@pytest.mark.anyio
async def test_stream_idle(stream: HTTPStream) -> None:
    assert stream.idle is False


@pytest.mark.anyio
async def test_closure(stream: HTTPStream) -> None:
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=b"/?a=b",
            method="GET",
            state=ConnectionState({}),
        )
    )
    assert not stream.closed
    await stream.handle(StreamClosed(stream_id=1))
    assert stream.closed
    await stream.handle(StreamClosed(stream_id=1))
    assert stream.closed
    # It is important that the disconnect message has only been sent
    # once.
    assert stream.app_put.call_args_list == [call({"type": "http.disconnect"})]  # type: ignore[unresolved-attribute]


@pytest.mark.anyio
async def test_abnormal_close_logging() -> None:
    config = Config()
    config.accesslog = "-"
    config.statsd_host = "localhost:9125"
    # This exercises an issue where `HTTPStream` at one point called the statsd logger
    # with `response=None` when the statsd logger failed to handle it.
    config.set_statsd_logger_class(StatsdLogger)
    stream = HTTPStream(
        AsyncMock(),
        config,
        WorkerContext(None),
        AsyncMock(),
        None,
        None,
        AsyncMock(),
        1,
        None,
    )

    async with config.log:
        await stream.handle(
            Request(
                stream_id=1,
                http_version="2",
                headers=[],
                raw_path=b"/?a=b",
                method="GET",
                state=ConnectionState({}),
            )
        )
        await stream.handle(StreamClosed(stream_id=1))


@pytest.mark.anyio
async def test_trailers_without_te_do_not_crash(stream: HTTPStream) -> None:
    """Trailers as the first message, from a client that never asked for them.

    Nothing is sent - the client did not offer `te: trailers` - but closing the
    stream here used to read self.response, which no response had yet assigned,
    so the app got an AttributeError rather than a reply.
    """
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],  # no te: trailers
            raw_path=b"/",
            method="GET",
            state=ConnectionState({}),
        )
    )

    await stream.app_send({"type": "http.response.trailers", "headers": [(b"x", b"y")]})

    # Still awaiting a response rather than closed, so the app can go on to send one
    assert stream.state == ASGIHTTPState.REQUEST


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        (b"/caf%C3%A9", "/café"),  # percent-encoded UTF-8, decoded per the ASGI spec
        (b"/a%20b", "/a b"),
        (b"/x%2Fy", "/x/y"),
    ],
)
@pytest.mark.anyio
async def test_handle_request_percent_encoded_path(
    stream: HTTPStream, raw_path: bytes, expected: str
) -> None:
    """A valid escape sequence is decoded into the character it stands for."""
    await stream.handle(
        Request(
            stream_id=1,
            http_version="2",
            headers=[],
            raw_path=raw_path,
            method="GET",
            state=ConnectionState({}),
        )
    )
    scope = stream.task_group.spawn_app.call_args[0][2]  # type: ignore[attr-defined]
    assert scope["path"] == expected
    # The undecoded bytes stay available for apps that need them
    assert scope["raw_path"] == raw_path
