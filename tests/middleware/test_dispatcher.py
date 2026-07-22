"""Tests for dispatcher middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from anycorn.middleware.dispatcher import DispatcherMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from anycorn.typing import HTTPScope, Scope


@pytest.mark.anyio
async def test_dispatcher_middleware(http_scope: HTTPScope) -> None:
    class EchoFramework:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __call__(self, scope: Scope, _receive: Callable, send: Callable) -> None:
            scope = cast("HTTPScope", scope)
            response = f"{self.name}-{scope['root_path']}-{scope['path']}"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"%d" % len(response))],
                }
            )
            await send({"type": "http.response.body", "body": response.encode()})

    app = DispatcherMiddleware({"/api/x": EchoFramework("apix"), "/api": EchoFramework("api")})

    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    scope1: HTTPScope = {**http_scope, "path": "/api/x/b"}  # type: ignore[typeddict-item, typeddict-unknown-key]
    await app(scope1, None, send)  # type: ignore[arg-type]
    await app({**http_scope, "path": "/api/b"}, None, send)  # type: ignore[typeddict-item, typeddict-unknown-key]
    await app({**http_scope, "path": "/"}, None, send)  # type: ignore[typeddict-item, typeddict-unknown-key]

    # the caller's scope must not be mutated in place
    assert scope1["path"] == "/api/x/b"
    assert scope1["root_path"] == ""

    response1 = b"apix-/api/x-/api/x/b"
    response2 = b"api-/api-/api/b"
    assert sent_events == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"%d" % len(response1))],
        },
        {"type": "http.response.body", "body": response1},
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"%d" % len(response2))],
        },
        {"type": "http.response.body", "body": response2},
        {"type": "http.response.start", "status": 404, "headers": [(b"content-length", b"0")]},
        {"type": "http.response.body"},
    ]


class ScopeFramework:
    """A framework that handles scope-based events."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self, _scope: Scope, _receive: Callable, send: Callable) -> None:
        await send({"type": "lifespan.startup.complete"})


@pytest.mark.anyio
async def test_dispatcher_lifespan() -> None:
    app = DispatcherMiddleware({"/apix": ScopeFramework("apix"), "/api": ScopeFramework("api")})

    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    async def receive() -> dict:
        return {"type": "lifespan.shutdown"}

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    assert sent_events == [{"type": "lifespan.startup.complete"}]
