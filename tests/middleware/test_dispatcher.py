"""Tests for dispatcher middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import anyio
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


class NoLifespanFramework:
    """A framework that declines lifespan the ASGI-sanctioned way: by raising."""

    async def __call__(self, scope: Scope, _receive: Callable, _send: Callable) -> None:
        msg = f"{scope['type']} protocol is not supported"
        raise ValueError(msg)


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
    # Each mount acked startup but returned without handling shutdown; the dispatcher
    # completes shutdown on their behalf so the caller's lifespan is not left waiting.
    assert sent_events == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


@pytest.mark.anyio
async def test_dispatcher_lifespan_with_a_mount_that_declines() -> None:
    """A mounted app that doesn't support lifespan must not block or crash the others.

    Declining lifespan by raising used to propagate out of the task group and take
    the whole dispatcher down; an app that instead just returned without acking left
    the dispatcher waiting on a startup.complete that never came. Either way, the
    dispatcher now completes that mount on its behalf.

    https://github.com/pgjones/hypercorn/issues/55
    https://github.com/pgjones/hypercorn/issues/315
    """
    app = DispatcherMiddleware({"/api": ScopeFramework("api"), "/legacy": NoLifespanFramework()})

    sent_events = []

    async def send(message: dict) -> None:
        sent_events.append(message)

    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive() -> dict:
        return next(messages)

    with anyio.fail_after(2):
        await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)

    assert sent_events == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


class FailingLifespanFramework:
    """A framework whose startup genuinely fails, the ASGI way: by saying so."""

    async def __call__(self, _scope: Scope, receive: Callable, send: Callable) -> None:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.failed", "message": "no database"})


@pytest.mark.anyio
async def test_dispatcher_lifespan_with_a_mount_that_fails_startup() -> None:
    """A mount that cannot start must fail the dispatcher, not be completed for.

    The failure used to be dropped - nothing forwarded it - and the mount was then
    completed on its way out, so a worker whose app had said it could not run
    reported itself started. Declining lifespan (raising, or returning without an
    ack) is still treated as having none, which the test above covers.
    """
    app = DispatcherMiddleware(
        {"/api": ScopeFramework("api"), "/broken": FailingLifespanFramework()}
    )

    sent_events = []

    async def send(message: dict) -> None:
        sent_events.append(message)

    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive() -> dict:
        return next(messages)

    with anyio.fail_after(2):
        await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)

    types = [event["type"] for event in sent_events]
    assert "lifespan.startup.failed" in types
    # Never reported ready: the failing mount is not completed for on its way out
    assert "lifespan.startup.complete" not in types
    failure = next(e for e in sent_events if e["type"] == "lifespan.startup.failed")
    assert failure["message"] == "no database"


@pytest.mark.anyio
async def test_dispatcher_denies_an_unmatched_websocket_with_404() -> None:
    """Where the server offers the denial response extension, say why - a 404.

    An http.response.start is not something a websocket peer can be sent, which is
    what an unmatched websocket used to get.
    """
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    app = DispatcherMiddleware({"/mounted": AsyncMock()})
    scope = {
        "type": "websocket",
        "path": "/elsewhere",
        "headers": [],
        "root_path": "",
        # As WSStream always advertises it
        "extensions": {"websocket.http.response": {}},
    }

    await app(scope, AsyncMock(), send)  # type: ignore[arg-type]

    assert [message["type"] for message in sent] == [
        "websocket.http.response.start",
        "websocket.http.response.body",
    ]
    assert sent[0]["status"] == 404  # noqa: PLR2004


@pytest.mark.anyio
async def test_dispatcher_closes_an_unmatched_websocket_without_the_extension() -> None:
    """Without it, closing is all a websocket peer can be told."""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    app = DispatcherMiddleware({"/mounted": AsyncMock()})
    scope = {"type": "websocket", "path": "/elsewhere", "headers": [], "root_path": ""}

    await app(scope, AsyncMock(), send)  # type: ignore[arg-type]

    assert [message["type"] for message in sent] == ["websocket.close"]


def _recording_mounts(served: list[tuple[str, str]]) -> Callable:
    def _mount(name: str) -> Callable:
        async def _app(scope: HTTPScope, _receive: Callable, send: Callable) -> None:
            served.append((name, scope["root_path"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return _app

    return _mount


@pytest.mark.anyio
async def test_strict_paths_matches_on_a_path_boundary(http_scope: HTTPScope) -> None:
    """With strict_paths a /foo mount must not swallow /foobar, which has its own."""
    served: list[tuple[str, str]] = []
    _mount = _recording_mounts(served)
    app = DispatcherMiddleware(
        {"/foo": _mount("foo"), "/foobar": _mount("foobar")}, strict_paths=True
    )

    for path, expected in (("/foobar", "foobar"), ("/foo", "foo"), ("/foo/deeper", "foo")):
        scope = dict(http_scope)
        scope["path"] = path
        scope["root_path"] = ""
        await app(scope, AsyncMock(), AsyncMock())  # type: ignore[arg-type]
        assert served[-1][0] == expected, f"{path} went to {served[-1][0]}"


@pytest.mark.anyio
async def test_prefix_matching_is_still_the_default(http_scope: HTTPScope) -> None:
    """Left alone, mounts still match on any shared prefix.

    Which is what routes callers today, so a mount relying on it keeps being
    reached; strict_paths is how the boundary behaviour is asked for.
    """
    served: list[tuple[str, str]] = []
    _mount = _recording_mounts(served)
    app = DispatcherMiddleware({"/foo": _mount("foo"), "/foobar": _mount("foobar")})

    scope = dict(http_scope)
    scope["path"] = "/foobar"
    scope["root_path"] = ""
    await app(scope, AsyncMock(), AsyncMock())  # type: ignore[arg-type]

    assert served == [("foo", "/foo")]


@pytest.mark.anyio
async def test_strict_paths_still_lets_a_root_mount_match_everything(
    http_scope: HTTPScope,
) -> None:
    """A "/" mount is a prefix of every path, and stays one under strict_paths."""
    served: list[tuple[str, str]] = []
    app = DispatcherMiddleware({"/": _recording_mounts(served)("root")}, strict_paths=True)

    scope = dict(http_scope)
    scope["path"] = "/anything/at/all"
    scope["root_path"] = ""
    await app(scope, AsyncMock(), AsyncMock())  # type: ignore[arg-type]

    assert served == [("root", "/")]
