from __future__ import annotations

from functools import partial
from typing import Callable

from ..typing import ASGIFramework, ASGIReceiveEvent, Scope

MAX_QUEUE_SIZE = 10


class _DispatcherMiddleware:
    def __init__(self, mounts: dict[str, ASGIFramework]) -> None:
        self.mounts = mounts

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        else:
            for path, app in self.mounts.items():
                if scope["path"].startswith(path):
                    scope["path"] = scope["path"][len(path) :] or "/"
                    return await app(scope, receive, send)
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body"})

    async def _handle_lifespan(self, scope: Scope, receive: Callable, send: Callable) -> None:
        pass


class DispatcherMiddleware(_DispatcherMiddleware):
    async def _handle_lifespan(self, scope: Scope, receive: Callable, send: Callable) -> None:
        import anyio

        self.app_queues: dict[
            str,
            tuple[
                anyio.streams.memory.MemoryObjectSendStream,
                anyio.streams.memory.MemoryObjectReceiveStream,
            ],
        ] = {
            path: anyio.create_memory_object_stream[ASGIReceiveEvent](MAX_QUEUE_SIZE)
            for path in self.mounts
        }
        self.startup_complete = {path: False for path in self.mounts}
        self.shutdown_complete = {path: False for path in self.mounts}

        async with anyio.create_task_group() as tg:
            for path, app in self.mounts.items():
                tg.start_soon(
                    app,
                    scope,
                    self.app_queues[path][1].receive,
                    partial(self.send, path, send),
                )

            while True:
                message = await receive()
                for channels in self.app_queues.values():
                    await channels[0].send(message)
                if message["type"] == "lifespan.shutdown":
                    break

    async def send(self, path: str, send: Callable, message: dict) -> None:
        if message["type"] == "lifespan.startup.complete":
            self.startup_complete[path] = True
            if all(self.startup_complete.values()):
                await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown.complete":
            self.shutdown_complete[path] = True
            if all(self.shutdown_complete.values()):
                await send({"type": "lifespan.shutdown.complete"})
