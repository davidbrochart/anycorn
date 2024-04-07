from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Awaitable, Callable

import anyio

from .config import Config
from .typing import AppWrapper, ASGIReceiveCallable, ASGIReceiveEvent, ASGISendEvent, Scope

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


async def _handle(
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
    except Exception:
        await config.log.exception("Error in ASGI Framework")
    finally:
        await send(None)


class TaskGroup:
    def __init__(self) -> None:
        self._task_group: anyio.abc.TaskGroup | None = None

    async def spawn_app(
        self,
        app: AppWrapper,
        config: Config,
        scope: Scope,
        send: Callable[[ASGISendEvent | None], Awaitable[None]],
    ) -> Callable[[ASGIReceiveEvent], Awaitable[None]]:
        app_send_channel, app_receive_channel = anyio.create_memory_object_stream[Any](
            config.max_app_queue_size
        )
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

    def spawn(self, func: Callable, *args: Any) -> None:
        self._task_group.start_soon(func, *args)

    async def __aenter__(self) -> TaskGroup:
        async with AsyncExitStack() as exit_stack:
            tg = anyio.create_task_group()
            self._task_group = await exit_stack.enter_async_context(tg)
            self._exit_stack = exit_stack.pop_all()
        return self

    async def __aexit__(self, exc_type: type, exc_value: BaseException, tb: TracebackType) -> Any:
        self._task_group = None
        return await self._exit_stack.__aexit__(exc_type, exc_value, tb)
