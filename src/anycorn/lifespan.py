"""ASGI Lifespan protocol implementation."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import anyio
import anyio.abc
import anyio.from_thread
import anyio.to_thread

from .typing import AppWrapper, ASGIReceiveEvent, ASGISendEvent, LifespanScope, LifespanState
from .utils import LifespanFailureError, LifespanTimeoutError

if TYPE_CHECKING:
    from .config import Config

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


class UnexpectedMessageError(Exception):
    """Raised when an unexpected ASGI lifespan message is received."""


class Lifespan:
    """Manages the ASGI lifespan protocol for startup and shutdown."""

    def __init__(self, app: AppWrapper, config: Config, state: LifespanState) -> None:
        self.app = app
        self.config = config
        self.startup = anyio.Event()
        self.shutdown = anyio.Event()
        self.app_send_channel, self.app_receive_channel = anyio.create_memory_object_stream[
            ASGIReceiveEvent
        ](config.max_app_queue_size)
        self.state = state
        self.supported = True

    async def handle_lifespan(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Run the ASGI lifespan event loop."""
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
                    raise reraise_error from error

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
        """Send the startup event and wait for the application to complete startup."""
        if not self.supported:
            return

        await self.app_send_channel.send({"type": "lifespan.startup"})
        try:
            with anyio.fail_after(self.config.startup_timeout):
                await self.startup.wait()
        except TimeoutError as error:
            msg = "startup"
            raise LifespanTimeoutError(msg) from error

    async def wait_for_shutdown(self) -> None:
        """Send the shutdown event and wait for the application to complete shutdown."""
        if not self.supported:
            return

        await self.app_send_channel.send({"type": "lifespan.shutdown"})
        try:
            with anyio.fail_after(self.config.shutdown_timeout):
                await self.shutdown.wait()
        except TimeoutError as error:
            msg = "startup"
            raise LifespanTimeoutError(msg) from error

    async def asgi_receive(self) -> ASGIReceiveEvent:
        """Receive the next ASGI event from the lifespan channel."""
        return await self.app_receive_channel.receive()

    async def asgi_send(self, message: ASGISendEvent) -> None:
        """Process an ASGI send message from the application."""
        if message["type"] == "lifespan.startup.complete":
            self.startup.set()
        elif message["type"] == "lifespan.shutdown.complete":
            self.shutdown.set()
        elif message["type"] == "lifespan.startup.failed":
            msg = "startup"
            raise LifespanFailureError(msg, message.get("message", ""))
        elif message["type"] == "lifespan.shutdown.failed":
            msg = "shutdown"
            raise LifespanFailureError(msg, message.get("message", ""))
        else:
            raise UnexpectedMessageError(message["type"])
