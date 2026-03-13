"""Task group implementation wrapping anyio for concurrent ASGI request handling."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import anyio.from_thread
import anyio.to_thread
from typing_extensions import Self

from .typing import AppWrapper, ASGIReceiveCallable, ASGIReceiveEvent, ASGISendEvent, Scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from .config import Config

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


async def _handle(  # noqa: PLR0913
    app: AppWrapper,
    config: Config,
    scope: Scope,
    receive: ASGIReceiveCallable,
    send: Callable[[ASGISendEvent | None], Awaitable[None]],
    sync_spawn: Callable,
    call_soon: Callable,
) -> None:
    try:
        await app(scope, receive, send, sync_spawn, call_soon)
    except anyio.get_cancelled_exc_class():
        raise
    except BaseExceptionGroup as error:
        _, other_errors = error.split(anyio.get_cancelled_exc_class())
        if other_errors is not None:
            await config.log.exception("Error in ASGI Framework")
            await send(None)
        else:
            raise
    except Exception:  # noqa: BLE001
        await config.log.exception("Error in ASGI Framework")
    finally:
        await send(None)


class TaskGroup:
    """Manages concurrent ASGI app tasks using an anyio task group."""

    def __init__(self) -> None:
        self._task_group: anyio.abc.TaskGroup | None = None
    async def spawn_app(
        self,
        app: AppWrapper,
        config: Config,
        scope: Scope,
        send: Callable[[ASGISendEvent | None], Awaitable[None]],
    ) -> Callable[[ASGIReceiveEvent], Awaitable[None]]:
        """Spawn an ASGI app task and return a callable to send receive events to it."""
        app_send_channel, app_receive_channel = anyio.create_memory_object_stream[ASGIReceiveEvent](
            config.max_app_queue_size
        )
        assert self._task_group is not None
        self._task_group.start_soon(
            _handle,
            app,
            config,
            scope,
            app_receive_channel.receive,
            send,
            anyio.to_thread.run_sync,
            anyio.from_thread.run,
        )
        return app_send_channel.send

    def spawn(self, func: Callable, *args: Any) -> None:  # noqa: ANN401
        """Spawn an arbitrary coroutine function in the task group."""
        assert self._task_group is not None
        self._task_group.start_soon(func, *args)

    async def __aenter__(self) -> Self:
        tg = anyio.create_task_group()
        self._task_group = await tg.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._task_group is not None
        await self._task_group.__aexit__(exc_type, exc_value, tb)
        self._task_group = None
