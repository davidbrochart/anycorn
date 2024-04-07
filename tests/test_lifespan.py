from __future__ import annotations

import sys

import anyio
import pytest
from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.lifespan import Lifespan
from anycorn.utils import LifespanFailureError, LifespanTimeoutError

from .helpers import SlowLifespanFramework, lifespan_failure

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup


@pytest.mark.anyio
async def test_startup_timeout_error() -> None:
    config = Config()
    config.startup_timeout = 0.01
    lifespan = Lifespan(ASGIWrapper(SlowLifespanFramework(0.02, anyio.sleep)), config)
    async with anyio.create_task_group() as tg:
        tg.start_soon(lifespan.handle_lifespan)
        with pytest.raises(LifespanTimeoutError) as exc_info:
            await lifespan.wait_for_startup()
        assert str(exc_info.value).startswith("Timeout whilst awaiting startup")


@pytest.mark.anyio
async def test_startup_failure() -> None:
    lifespan = Lifespan(ASGIWrapper(lifespan_failure), Config())
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(lifespan.handle_lifespan)
            await lifespan.wait_for_startup()
            exception = None
    except Exception as e:
        exception = e

    assert exception is not None
    assert isinstance(exception, ExceptionGroup)
    assert len(exception.exceptions) == 1
    exception = exception.exceptions[0]
    assert isinstance(exception, LifespanFailureError)
    assert str(exception) == "Lifespan failure in startup. 'Failure'"
