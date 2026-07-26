"""Dispatcher middleware for routing ASGI requests to multiple sub-applications."""

from __future__ import annotations

from contextlib import AsyncExitStack
from functools import partial
from typing import TYPE_CHECKING

import anyio
import anyio.streams.memory

from anycorn.typing import ASGIFramework, ASGIReceiveEvent, Scope

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_QUEUE_SIZE = 10


class _DispatcherMiddleware:
    def __init__(self, mounts: dict[str, ASGIFramework], *, strict_paths: bool = False) -> None:
        self.mounts = mounts
        self.strict_paths = strict_paths

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        else:
            for path, app in self.mounts.items():
                if self._mounted_at(scope["path"], path):
                    local_scope = scope.copy()
                    local_scope["root_path"] = local_scope.get("root_path", "") + path
                    return await app(local_scope, receive, send)
            if scope["type"] == "websocket":
                await self._reject_websocket(scope, send)
            else:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body"})
        return None

    async def _reject_websocket(self, scope: Scope, send: Callable) -> None:
        """Say there is nothing mounted here, as fully as the server allows.

        An HTTP response is not something a websocket peer can be sent, so a 404
        needs the denial response extension. Where the server offers it the client
        learns why it was turned away; where it does not, closing is all there is.
        Mirrors HTTPToHTTPSRedirectMiddleware, which chooses the same way.
        """
        if "websocket.http.response" in scope.get("extensions", {}):
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 404,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "websocket.http.response.body"})
        else:
            await send({"type": "websocket.close"})

    def _mounted_at(self, path: str, mount: str) -> bool:
        """Return True when *path* belongs to the app mounted at *mount*.

        Matching on any shared prefix, which is what this does by default, lets a
        /foo mount swallow /foobar - leaving a /foobar mount unreachable, and
        handing /foo a root_path it never served. strict_paths matches on a path
        segment boundary instead, so each mount gets only what is under it. It is
        opt-in because the prefix behaviour is what callers have been routed by so
        far, and a mount relying on it would stop being reached.
        """
        if not self.strict_paths:
            return path.startswith(mount)
        return path == mount or path.startswith(mount.rstrip("/") + "/")

    async def _handle_lifespan(self, scope: Scope, receive: Callable, send: Callable) -> None:
        pass


class DispatcherMiddleware(_DispatcherMiddleware):
    """ASGI middleware that dispatches requests to different apps based on path prefixes."""

    async def _handle_lifespan(self, scope: Scope, receive: Callable, send: Callable) -> None:
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
        self.startup_complete = dict.fromkeys(self.mounts, False)
        self.shutdown_complete = dict.fromkeys(self.mounts, False)
        # Mounts that reported a failure, so nothing is later completed on their behalf
        self.failed = dict.fromkeys(self.mounts, False)

        async with AsyncExitStack() as stack:
            for send_stream, receive_stream in self.app_queues.values():
                await stack.enter_async_context(send_stream)
                await stack.enter_async_context(receive_stream)

            tg = await stack.enter_async_context(anyio.create_task_group())
            for path in self.mounts:
                tg.start_soon(self._run_mount, path, scope, send)

            while True:
                message = await receive()
                for channels in self.app_queues.values():
                    await channels[0].send(message)
                if message["type"] == "lifespan.shutdown":
                    break

    async def _run_mount(self, path: str, scope: Scope, send: Callable) -> None:
        forward = partial(self.send, path, send)
        try:
            await self.mounts[path](scope, self.app_queues[path][1].receive, forward)
        except Exception:
            # The ASGI-sanctioned way to decline lifespan is to raise. If that happens
            # before the app has acknowledged startup, treat this mount as having no
            # lifespan of its own rather than failing the whole dispatcher; a raise
            # once it is past startup is a genuine error, so let that propagate.
            # So is one from a mount that reported a failure: passing that on is what
            # raises here, and swallowing it leaves the server waiting out its startup
            # timeout for an acknowledgement that is never coming.
            if self.startup_complete[path] or self.failed[path]:
                raise
        # A mount that said it failed has had that passed on already; completing for
        # it here would take it back, and report a worker that cannot serve as ready.
        if self.failed[path]:
            return
        # However it opted out - by raising, or by returning without acknowledging -
        # make sure this mount does not leave the dispatcher's own startup or shutdown
        # waiting on a completion that will never come.
        if not self.startup_complete[path]:
            await forward({"type": "lifespan.startup.complete"})
        if not self.shutdown_complete[path]:
            await forward({"type": "lifespan.shutdown.complete"})

    async def send(self, path: str, send: Callable, message: dict) -> None:
        """Forward lifespan messages and track startup/shutdown completion across mounted apps."""
        if message["type"] == "lifespan.startup.complete":
            self.startup_complete[path] = True
            if all(self.startup_complete.values()):
                await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown.complete":
            self.shutdown_complete[path] = True
            if all(self.shutdown_complete.values()):
                await send({"type": "lifespan.shutdown.complete"})
        elif message["type"] in {"lifespan.startup.failed", "lifespan.shutdown.failed"}:
            # One mount that cannot start is the dispatcher failing to start. This
            # used to be dropped, and the mount then completed on its way out, so a
            # worker whose app had said it could not run reported itself ready.
            self.failed[path] = True
            await send(message)
