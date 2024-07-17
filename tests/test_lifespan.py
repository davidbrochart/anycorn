from __future__ import annotations

import sys

import anyio
import pytest
from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.lifespan import Lifespan
from anycorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope
from anycorn.utils import LifespanFailureError, LifespanTimeoutError

from .helpers import SlowLifespanFramework

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup


@pytest.mark.anyio
async def test_startup_timeout_error() -> None:
    config = Config()
    config.startup_timeout = 0.01
    lifespan = Lifespan(ASGIWrapper(SlowLifespanFramework(0.02, anyio.sleep)), config, {})
    async with anyio.create_task_group() as tg:
        tg.start_soon(lifespan.handle_lifespan)
        with pytest.raises(LifespanTimeoutError) as exc_info:
            await lifespan.wait_for_startup()
        assert str(exc_info.value).startswith("Timeout whilst awaiting startup")


async def _lifespan_failure(
    scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
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
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(lifespan.handle_lifespan)
            await lifespan.wait_for_startup()
    except ExceptionGroup as error:
        assert error.subgroup(LifespanFailureError) is not None
