"""Tests for ASGI lifespan protocol handling."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import anyio
import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.lifespan import Lifespan
from anycorn.utils import LifespanFailureError, LifespanTimeoutError

if TYPE_CHECKING:
    from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup


async def _never_startup_framework(
    _scope: Scope, receive: ASGIReceiveCallable, _send: ASGISendCallable
) -> None:
    """Receive the startup message but never acknowledge it, so startup times out."""
    await receive()
    await anyio.sleep_forever()


@pytest.mark.anyio
async def test_startup_timeout_error() -> None:
    config = Config()
    config.startup_timeout = 0.01
    lifespan = Lifespan(ASGIWrapper(_never_startup_framework), config, {})
    async with anyio.create_task_group() as tg:
        await tg.start(lifespan.handle_lifespan)
        with pytest.raises(LifespanTimeoutError) as exc_info:
            await lifespan.wait_for_startup()
        assert str(exc_info.value).startswith("Timeout whilst awaiting startup")
        # The app never completes startup, so cancel to release the lifespan task
        # instead of waiting on a sleep whose length raced the timeout on Windows.
        tg.cancel_scope.cancel()


async def _slow_shutdown_framework(
    _scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    """Complete startup promptly, then never acknowledge shutdown."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await anyio.sleep_forever()


@pytest.mark.anyio
async def test_shutdown_timeout_error() -> None:
    config = Config()
    config.shutdown_timeout = 0.01
    lifespan = Lifespan(ASGIWrapper(_slow_shutdown_framework), config, {})
    async with anyio.create_task_group() as tg:
        await tg.start(lifespan.handle_lifespan)
        await lifespan.wait_for_startup()
        with pytest.raises(LifespanTimeoutError) as exc_info:
            await lifespan.wait_for_shutdown()
        # The message must name the shutdown stage, not startup (it used to say
        # "startup" here from a copied timeout branch).
        assert str(exc_info.value).startswith("Timeout whilst awaiting shutdown")
        tg.cancel_scope.cancel()


async def _lifespan_failure(
    _scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
) -> None:
    async with anyio.create_task_group():
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.failed", "message": "Failure"})
            break


@pytest.mark.anyio
async def test_startup_failure() -> None:
    lifespan = Lifespan(ASGIWrapper(_lifespan_failure), Config(), {})
    with pytest.raises(ExceptionGroup) as exc_info:  # noqa: PT012
        async with anyio.create_task_group() as tg:
            await tg.start(lifespan.handle_lifespan)
            await lifespan.wait_for_startup()
    assert exc_info.value.subgroup(LifespanFailureError) is not None
