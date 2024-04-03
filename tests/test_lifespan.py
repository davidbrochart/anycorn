from __future__ import annotations

import anyio
import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.lifespan import Lifespan
from anycorn.utils import LifespanFailureError, LifespanTimeoutError
from .helpers import lifespan_failure, SlowLifespanFramework


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
    with pytest.raises(LifespanFailureError) as exc_info:
        async with anyio.create_task_group() as lifespan_tg:
            await lifespan_tg.start(lifespan.handle_lifespan)
            await lifespan.wait_for_startup()

    assert str(exc_info.value) == "Lifespan failure in startup. 'Failure'"
