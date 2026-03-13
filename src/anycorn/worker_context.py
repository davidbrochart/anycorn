"""Worker context and event/task wrappers for anyio-based workers."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, ClassVar

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    """Manages a single restartable background task using an anyio CancelScope."""

    def __init__(self) -> None:
        self._handle: anyio.CancelScope | None = None
        self._lock = anyio.Lock()

    async def restart(self, task_group: TaskGroup, action: Callable) -> None:
        """Cancel any running task and start *action* as the new task."""
        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
            self._handle = await task_group._task_group.start(_cancel_wrapper(action))  # type: ignore[attr-defined]  # noqa: SLF001

    async def stop(self) -> None:
        """Cancel and clear the current task, if any."""
        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
            self._handle = None


class EventWrapper:
    """Async event primitive wrapping anyio.Event with a clear() operation."""

    def __init__(self) -> None:
        self._event = anyio.Event()

    async def clear(self) -> None:
        """Reset the event to the unset state."""
        self._event = anyio.Event()

    async def wait(self) -> None:
        """Wait until the event is set."""
        await self._event.wait()

    async def set(self) -> None:
        """Set the event, waking any waiters."""
        self._event.set()

    def is_set(self) -> bool:
        """Return True if the event has been set."""
        return self._event.is_set()


class WorkerContext:
    """Shared state for a single worker, tracking requests and shutdown signals."""

    event_class: ClassVar[type[Event]] = EventWrapper  # type: ignore[assignment]
    single_task_class: ClassVar[type[SingleTask]] = AnyioSingleTask  # type: ignore[assignment]

    def __init__(self, max_requests: int | None) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.terminate = self.event_class()
        self.terminated = self.event_class()

    async def mark_request(self) -> None:
        """Increment the request counter and signal termination if the limit is reached."""
        if self.max_requests is None:
            return

        self.requests += 1
        if self.requests > self.max_requests:
            await self.terminate.set()

    @staticmethod
    async def sleep(wait: float) -> None:
        """Sleep for *wait* seconds."""
        return await anyio.sleep(wait)

    @staticmethod
    def time() -> float:
        """Return the current event-loop time."""
        return anyio.current_time()
