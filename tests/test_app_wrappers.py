"""Tests for ASGI and WSGI application wrapper functionality."""

from __future__ import annotations

import math
import sys
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.from_thread
import anyio.to_thread
import pytest

from anycorn.app_wrappers import InvalidPathError, WSGIWrapper, _build_environ
from anycorn.typing import (
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendEvent,
    ConnectionState,
    HTTPScope,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def echo_body(environ: dict, start_response: Callable) -> list[bytes]:
    status = "200 OK"
    output = environ["wsgi.input"].read()
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(output))),
    ]
    start_response(status, headers)
    return [output]


@pytest.mark.anyio
async def test_wsgi() -> None:
    app = WSGIWrapper(echo_body, 2**16)
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"a=b",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    send_channel, receive_channel = anyio.create_memory_object_stream[ASGIReceiveEvent](1)

    messages = []

    async def _send(message: ASGISendEvent) -> None:
        nonlocal messages
        messages.append(message)

    async with send_channel, receive_channel:
        await send_channel.send({"type": "http.request"})  # type: ignore[arg-type, misc]
        receive = cast("ASGIReceiveCallable", receive_channel.receive)
        await app(scope, receive, _send, anyio.to_thread.run_sync, anyio.from_thread.run)
    assert messages == [
        {
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", b"0"),
            ],
            "status": 200,
            "type": "http.response.start",
        },
        # No body message for the app's empty b"" chunk; the final empty body below
        # is the caller (handle_http) closing the response.
        {"body": b"", "type": "http.response.body", "more_body": False},
    ]


async def _run_app(app: WSGIWrapper, scope: HTTPScope, body: bytes = b"") -> list[ASGISendEvent]:
    send_stream, recv_stream = anyio.create_memory_object_stream[dict](math.inf)

    messages = []

    async def _send(message: ASGISendEvent) -> None:
        nonlocal messages
        messages.append(message)

    def _call_soon(func: Callable, *args: Any) -> Any:  # noqa: ANN401
        return anyio.from_thread.run(func, *args)

    async with send_stream, recv_stream:
        await send_stream.send({"type": "http.request", "body": body})
        receive = cast("ASGIReceiveCallable", recv_stream.receive)
        await app(scope, receive, _send, anyio.to_thread.run_sync, _call_soon)
    return messages


@pytest.mark.anyio
async def test_wsgi2() -> None:
    app = WSGIWrapper(echo_body, 2**16)
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"a=b",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    messages = await _run_app(app, scope)
    assert messages == [
        {
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", b"0"),
            ],
            "status": 200,
            "type": "http.response.start",
        },
        # No body message for the app's empty b"" chunk; the final empty body below
        # is the caller (handle_http) closing the response.
        {"body": b"", "type": "http.response.body", "more_body": False},
    ]


@pytest.mark.anyio
async def test_wsgi_lifespan_handshake() -> None:
    """A WSGI app has no lifespan of its own, but must still ack the ASGI handshake.

    Returning immediately without ever awaiting receive() (the previous behaviour)
    left the caller's Lifespan.supported permanently True - since that only flips
    to False when the app *raises* - while never actually reading the startup
    message Lifespan.wait_for_startup() goes on to send.
    """
    app = WSGIWrapper(echo_body, 2**16)
    to_app_send, to_app_receive = anyio.create_memory_object_stream[ASGIReceiveEvent](1)
    from_app_send, from_app_receive = anyio.create_memory_object_stream[ASGISendEvent](1)

    async def _send(message: ASGISendEvent) -> None:
        await from_app_send.send(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

    async with (
        anyio.create_task_group() as tg,
        to_app_send,
        to_app_receive,
        from_app_send,
        from_app_receive,
    ):
        receive = cast("ASGIReceiveCallable", to_app_receive.receive)
        tg.start_soon(
            app,
            scope,
            receive,
            _send,
            anyio.to_thread.run_sync,
            anyio.from_thread.run,
        )

        with anyio.fail_after(2):
            await to_app_send.send({"type": "lifespan.startup"})
            assert await from_app_receive.receive() == {"type": "lifespan.startup.complete"}

            await to_app_send.send({"type": "lifespan.shutdown"})
            assert await from_app_receive.receive() == {"type": "lifespan.shutdown.complete"}


@pytest.mark.anyio
async def test_max_body_size() -> None:
    app = WSGIWrapper(echo_body, 4)
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"a=b",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    messages = await _run_app(app, scope, b"abcde")
    assert messages == [
        {"headers": [], "status": 400, "type": "http.response.start"},
        {"body": bytearray(b""), "type": "http.response.body", "more_body": False},
    ]


def no_start_response(_environ: dict, _start_response: Callable) -> list[bytes]:
    return [b"result"]


@pytest.mark.anyio
async def test_no_start_response() -> None:
    app = WSGIWrapper(no_start_response, 2**16)
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"a=b",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    with pytest.raises(RuntimeError):
        await _run_app(app, scope)


def deferred_start_response(_environ: dict, start_response: Callable) -> Iterator[bytes]:
    # A generator function's body, including any call to start_response, does not
    # run until the generator is first iterated - not when it is called.
    start_response("200 OK", [("Content-Length", "5")])
    yield b"hello"


@pytest.mark.anyio
async def test_wsgi_generator_app_defers_start_response() -> None:
    """A generator-based WSGI app must not raise before it is ever iterated."""
    app = WSGIWrapper(deferred_start_response, 2**16)
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"a=b",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    messages = await _run_app(app, scope)
    assert messages == [
        {
            "headers": [(b"content-length", b"5")],
            "status": 200,
            "type": "http.response.start",
        },
        {"body": b"hello", "type": "http.response.body", "more_body": True},
        {"body": b"", "type": "http.response.body", "more_body": False},
    ]


def _http_scope() -> HTTPScope:
    return {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "/",
        "query_string": b"",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }


def empty_body_app(_environ: dict, start_response: Callable) -> list[bytes]:
    # A HEAD handler, a 204/304, or any app that returns an empty iterable.
    start_response("204 No Content", [("Content-Length", "0")])
    return []


@pytest.mark.anyio
async def test_wsgi_empty_iterable_still_starts_the_response() -> None:
    """An app that yields no body must still emit http.response.start.

    Gating the start on the first chunk means an empty iterable never starts the
    response; the caller's following body message then crashes the stream with
    UnexpectedMessageError. Flush the headers once the iterable is exhausted instead.

    https://github.com/pgjones/hypercorn/issues/331
    """
    app = WSGIWrapper(empty_body_app, 2**16)
    messages = await _run_app(app, _http_scope())
    assert messages == [
        {"type": "http.response.start", "status": 204, "headers": [(b"content-length", b"0")]},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]


def empty_then_data_app(_environ: dict, start_response: Callable) -> Iterator[bytes]:
    start_response("200 OK", [("Content-Length", "5")])
    yield b""  # not data: must not start the response or produce a body message
    yield b"hello"


@pytest.mark.anyio
async def test_wsgi_empty_chunk_does_not_start_the_response() -> None:
    """An empty bytestring is not body data, so it must not flush headers.

    https://github.com/pgjones/hypercorn/issues/331
    """
    app = WSGIWrapper(empty_then_data_app, 2**16)
    messages = await _run_app(app, _http_scope())
    assert messages == [
        {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"5")]},
        {"type": "http.response.body", "body": b"hello", "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]


def double_start_response_app(_environ: dict, start_response: Callable) -> list[bytes]:
    start_response("200 OK", [])
    start_response("500 Internal Server Error", [])  # second call, no exc_info
    return [b"x"]


@pytest.mark.anyio
async def test_wsgi_double_start_response_without_exc_info_raises() -> None:
    """PEP 3333: a second start_response without exc_info is an application error.

    https://github.com/pgjones/hypercorn/issues/331
    """
    app = WSGIWrapper(double_start_response_app, 2**16)
    with pytest.raises(AssertionError, match="more than once"):
        await _run_app(app, _http_scope())


def replace_after_send_app(_environ: dict, start_response: Callable) -> Iterator[bytes]:
    start_response("200 OK", [("Content-Length", "5")])
    yield b"hello"  # headers are flushed here
    try:
        raise ValueError("boom")  # noqa: TRY301
    except ValueError:
        # Reporting an error once headers are already sent must re-raise it, not
        # silently replace the response (PEP 3333).
        start_response("500 Internal Server Error", [], sys.exc_info())
    yield b"unreached"


@pytest.mark.anyio
async def test_wsgi_start_response_with_exc_info_after_headers_reraises() -> None:
    """start_response(exc_info=...) after headers are sent re-raises the error.

    https://github.com/pgjones/hypercorn/issues/331
    """
    app = WSGIWrapper(replace_after_send_app, 2**16)
    with pytest.raises(ValueError, match="boom"):
        await _run_app(app, _http_scope())


def test_build_environ_encoding() -> None:
    scope: HTTPScope = {
        "http_version": "1.0",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/中/文",
        "root_path": "/中",
        "query_string": b"bar=baz",
        "raw_path": "/中/文".encode(),
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    environ = _build_environ(scope, b"")
    assert environ["SCRIPT_NAME"] == "/中".encode().decode("latin-1")
    assert environ["PATH_INFO"] == "/文".encode().decode("latin-1")


def test_build_environ_server_port_is_a_string() -> None:
    """PEP 3333: environ values are native strings, so SERVER_PORT is not a raw int."""
    scope: HTTPScope = {
        "http_version": "1.1",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "raw_path": b"/",
        "scheme": "http",
        "type": "http",
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
        "extensions": {},
        "state": ConnectionState({}),
    }
    environ = _build_environ(scope, b"")
    assert environ["SERVER_PORT"] == "8000"
    assert isinstance(environ["SERVER_PORT"], str)


def test_build_environ_root_path() -> None:
    scope: HTTPScope = {
        "http_version": "1.0",
        "asgi": {},
        "method": "GET",
        "headers": [],
        "path": "/中文",
        "root_path": "/中国",
        "query_string": b"bar=baz",
        "raw_path": "/中文".encode(),
        "scheme": "http",
        "type": "http",
        "client": ("localhost", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    with pytest.raises(InvalidPathError):
        _build_environ(scope, b"")
