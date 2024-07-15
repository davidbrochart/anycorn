from __future__ import annotations

from functools import wraps
from typing import Awaitable, Callable

import anyio
from anyio import current_time, sleep

from .typing import Event, SingleTask, TaskGroup


def _cancel_wrapper(func: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    @wraps(func)
    async def wrapper(
        task_status: anyio.abc.TaskStatus[anyio.CancelScope] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        cancel_scope = anyio.CancelScope()
        task_status.started(cancel_scope)
        with cancel_scope:
            await func()

    return wrapper


class AnyioSingleTask:
    def __init__(self) -> None:
        self._handle: anyio.CancelScope | None = None
        self._lock = anyio.Lock()

    async def restart(self, task_group: TaskGroup, action: Callable) -> None:
        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
            self._handle = await task_group._task_group.start(_cancel_wrapper(action))  # type: ignore[attr-defined]

    async def stop(self) -> None:
        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
            self._handle = None


class EventWrapper:
    def __init__(self) -> None:
        self._event = anyio.Event()

    async def clear(self) -> None:
        self._event = anyio.Event()

    async def wait(self) -> None:
        await self._event.wait()

    async def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class WorkerContext:
    event_class: type[Event] = EventWrapper
    single_task_class: type[SingleTask] = AnyioSingleTask

    def __init__(self, max_requests: int | None) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.terminate = self.event_class()
        self.terminated = self.event_class()

    async def mark_request(self) -> None:
        if self.max_requests is None:
            return

        self.requests += 1
        if self.requests > self.max_requests:
            await self.terminate.set()

    @staticmethod
    async def sleep(wait: float | int) -> None:
        return await sleep(wait)

    @staticmethod
    def time() -> float:
        return current_time()
