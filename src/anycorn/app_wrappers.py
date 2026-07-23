"""Wrappers for ASGI and WSGI applications."""

from __future__ import annotations

import sys
from functools import partial
from io import BytesIO
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from .typing import (
        ASGIFramework,
        ASGIReceiveCallable,
        ASGISendCallable,
        HTTPScope,
        Scope,
        WSGIFramework,
    )


@runtime_checkable
class _SupportsClose(Protocol):
    """A WSGI iterable that carries the optional close() hook from PEP 3333."""

    def close(self) -> None: ...


class InvalidPathError(Exception):
    """Raised when the request path is invalid."""


class ASGIWrapper:
    """Wrapper that adapts an ASGI application to the internal AppWrapper protocol."""

    def __init__(self, app: ASGIFramework) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,  # noqa: ARG002
        call_soon: Callable,  # noqa: ARG002
    ) -> None:
        """Call the wrapped ASGI application."""
        await self.app(scope, receive, send)


class WSGIWrapper:
    """Wrapper that adapts a WSGI application to the internal AppWrapper protocol."""

    def __init__(self, app: WSGIFramework, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
        call_soon: Callable,
    ) -> None:
        """Dispatch a request to the wrapped WSGI application."""
        if scope["type"] == "http":
            await self.handle_http(scope, receive, send, sync_spawn, call_soon)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close"})  # type: ignore[arg-type, misc]
        elif scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
        else:
            msg = f"Unknown scope type, {scope['type']}"
            raise RuntimeError(msg)

    async def _handle_lifespan(self, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        """Acknowledge the ASGI lifespan protocol; a WSGI app has no lifespan of its own.

        Returning immediately without ever awaiting receive() here (as this used to)
        leaves Lifespan.supported still True, since that only flips to False when the
        app *raises* - so wait_for_startup() goes on to send into a channel this
        wrapper never reads from, which Lifespan's cleanup has by then already closed.
        """
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def handle_http(
        self,
        scope: HTTPScope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        sync_spawn: Callable,
        call_soon: Callable,
    ) -> None:
        """Handle an HTTP request via the WSGI application."""
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_size:
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            if not message.get("more_body"):
                break

        try:
            environ = _build_environ(scope, body)  # type: ignore[arg-type]
        except InvalidPathError:
            await send({"type": "http.response.start", "status": 404, "headers": []})
        else:
            await sync_spawn(self.run_app, environ, partial(call_soon, send))
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    def run_app(self, environ: dict, send: Callable) -> None:  # noqa: C901
        """Run the WSGI app and forward the response via *send*."""
        status_code: int | None = None
        headers: list[tuple[bytes, bytes]] = []
        headers_sent = False

        def start_response(
            status: str,
            response_headers: list[tuple[str, str]],
            exc_info: tuple[type[BaseException], BaseException, TracebackType] | None = None,
        ) -> None:
            nonlocal status_code, headers

            if exc_info is not None:
                try:
                    if headers_sent:
                        # Too late to change the response, so surface the error the
                        # app is reporting rather than swallow it (PEP 3333).
                        raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None  # break the traceback reference cycle
            elif status_code is not None:
                msg = "start_response() called more than once without exc_info"
                raise AssertionError(msg)

            raw, _ = status.split(" ", 1)
            status_code = int(raw)
            headers = [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in response_headers
            ]

        response_body = self.app(environ, start_response)

        def send_start() -> None:
            nonlocal headers_sent
            if headers_sent:
                return
            if status_code is None:
                msg = "WSGI app did not call start_response"
                raise RuntimeError(msg)
            send({"type": "http.response.start", "status": status_code, "headers": headers})
            headers_sent = True

        try:
            for output in response_body:
                # PEP 3333: headers are not sent until there is body data to send - an
                # empty bytestring is not data, and holding off past it leaves the app
                # free to replace the status/headers via start_response(exc_info=...).
                if output:
                    send_start()
                    send({"type": "http.response.body", "body": output, "more_body": True})
            # The iterable is exhausted: flush the headers even when no body was
            # produced (a HEAD handler, a 204/304, or any empty iterable), so the
            # response still starts rather than crashing the stream.
            send_start()
        finally:
            if isinstance(response_body, _SupportsClose):
                response_body.close()


def _build_environ(scope: HTTPScope, body: bytes) -> dict:
    server = scope.get("server") or ("localhost", 80)
    path = scope["path"]
    script_name = scope.get("root_path", "")
    if path.startswith(script_name):
        path = path[len(script_name) :]
        path = path if path != "" else "/"
    else:
        raise InvalidPathError

    environ = {
        "REQUEST_METHOD": scope["method"],
        "SCRIPT_NAME": script_name.encode("utf8").decode("latin1"),
        "PATH_INFO": path.encode("utf8").decode("latin1"),
        "QUERY_STRING": scope["query_string"].decode("ascii"),
        "SERVER_NAME": server[0],
        "SERVER_PORT": server[1],
        "SERVER_PROTOCOL": f"HTTP/{scope['http_version']}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": BytesIO(body),
        "wsgi.errors": sys.stdout,
        "wsgi.multithread": True,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
    }

    client = scope.get("client")
    if client is not None:
        environ["REMOTE_ADDR"] = client[0]

    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin1")
        if name == "content-length":
            corrected_name = "CONTENT_LENGTH"
        elif name == "content-type":
            corrected_name = "CONTENT_TYPE"
        else:
            corrected_name = f"HTTP_{name.upper().replace('-', '_')}"
        # HTTPbis say only ASCII chars are allowed in headers, but we latin1 just in case
        value = raw_value.decode("latin1")
        if corrected_name in environ:
            value = environ[corrected_name] + "," + value  # type: ignore[operator,assignment]
        environ[corrected_name] = value
    return environ
