from __future__ import annotations

from collections.abc import Iterable
from typing import Callable

from ..app_wrappers import WSGIWrapper
from ..typing import ASGIReceiveCallable, ASGISendCallable, Scope, WSGIFramework

MAX_BODY_SIZE = 2**16

WSGICallable = Callable[[dict, Callable], Iterable[bytes]]


class InvalidPathError(Exception):
    pass


class _WSGIMiddleware:
    def __init__(self, wsgi_app: WSGIFramework, max_body_size: int = MAX_BODY_SIZE) -> None:
        self.wsgi_app = WSGIWrapper(wsgi_app, max_body_size)
        self.max_body_size = max_body_size

    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        pass


class WSGIMiddleware(_WSGIMiddleware):
    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        import anyio

        await self.wsgi_app(scope, receive, send, anyio.to_thread.run_sync, anyio.from_thread.run)
