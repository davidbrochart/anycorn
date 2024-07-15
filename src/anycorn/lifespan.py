from __future__ import annotations

import sys

import anyio
from anyio import TASK_STATUS_IGNORED, Event, create_memory_object_stream
from anyio.abc import TaskStatus

from .config import Config
from .typing import AppWrapper, ASGIReceiveEvent, ASGISendEvent, LifespanScope, LifespanState
from .utils import LifespanFailureError, LifespanTimeoutError

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


class UnexpectedMessageError(Exception):
    pass


class Lifespan:
    def __init__(self, app: AppWrapper, config: Config, state: LifespanState) -> None:
        self.app = app
        self.config = config
        self.startup = Event()
        self.shutdown = Event()
        self.app_send_channel, self.app_receive_channel = create_memory_object_stream[
            ASGIReceiveEvent
        ](config.max_app_queue_size)
        self.state = state
        self.supported = True

    async def handle_lifespan(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED) -> None:
        task_status.started()
        scope: LifespanScope = {
            "type": "lifespan",
            "asgi": {"spec_version": "2.0", "version": "3.0"},
            "state": self.state,
        }
        try:
            await self.app(
                scope,
                self.asgi_receive,
                self.asgi_send,
                anyio.to_thread.run_sync,
                anyio.from_thread.run,
            )
        except (LifespanFailureError, anyio.get_cancelled_exc_class()):
            raise
        except (BaseExceptionGroup, Exception) as error:
            if isinstance(error, BaseExceptionGroup):
                reraise_error = error.subgroup(
                    (LifespanFailureError, anyio.get_cancelled_exc_class())
                )
                if reraise_error is not None:
                    raise reraise_error

            self.supported = False
            if not self.startup.is_set():
                await self.config.log.warning(
                    "ASGI Framework Lifespan error, continuing without Lifespan support"
                )
            elif not self.shutdown.is_set():
                await self.config.log.exception(
                    "ASGI Framework Lifespan error, shutdown without Lifespan support"
                )
            else:
                await self.config.log.exception("ASGI Framework Lifespan errored after shutdown.")
        finally:
            self.startup.set()
            self.shutdown.set()
            await self.app_send_channel.aclose()
            await self.app_receive_channel.aclose()

    async def wait_for_startup(self) -> None:
        if not self.supported:
            return

        await self.app_send_channel.send({"type": "lifespan.startup"})
        try:
            with anyio.fail_after(self.config.startup_timeout):
                await self.startup.wait()
        except TimeoutError as error:
            raise LifespanTimeoutError("startup") from error

    async def wait_for_shutdown(self) -> None:
        if not self.supported:
            return

        await self.app_send_channel.send({"type": "lifespan.shutdown"})
        try:
            with anyio.fail_after(self.config.shutdown_timeout):
                await self.shutdown.wait()
        except TimeoutError as error:
            raise LifespanTimeoutError("startup") from error

    async def asgi_receive(self) -> ASGIReceiveEvent:
        return await self.app_receive_channel.receive()

    async def asgi_send(self, message: ASGISendEvent) -> None:
        if message["type"] == "lifespan.startup.complete":
            self.startup.set()
        elif message["type"] == "lifespan.shutdown.complete":
            self.shutdown.set()
        elif message["type"] == "lifespan.startup.failed":
            raise LifespanFailureError("startup", message.get("message", ""))
        elif message["type"] == "lifespan.shutdown.failed":
            raise LifespanFailureError("shutdown", message.get("message", ""))
        else:
            raise UnexpectedMessageError(message["type"])
