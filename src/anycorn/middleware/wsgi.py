"""Middleware for running WSGI applications inside an ASGI server via a thread executor."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import anyio.to_thread

from anycorn.app_wrappers import WSGIWrapper

if TYPE_CHECKING:
    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope, WSGIFramework

MAX_BODY_SIZE = 2**16

WSGICallable = Callable[[dict, Callable], Iterable[bytes]]


class InvalidPathError(Exception):
    """Raised when a WSGI app receives a request with an invalid path."""


class _WSGIMiddleware:
    def __init__(self, wsgi_app: WSGIFramework, max_body_size: int = MAX_BODY_SIZE) -> None:
        self.wsgi_app = WSGIWrapper(wsgi_app, max_body_size)
        self.max_body_size = max_body_size

    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        pass


class WSGIMiddleware(_WSGIMiddleware):
    """ASGI middleware that runs a WSGI application in a thread pool via anyio."""

    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        """Dispatch the ASGI request to the wrapped WSGI application running in a worker thread."""
        await self.wsgi_app(scope, receive, send, anyio.to_thread.run_sync, anyio.from_thread.run)
