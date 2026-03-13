"""TCP server implementation for handling incoming connections."""

from __future__ import annotations

import contextlib
from math import inf
from ssl import SSLError, SSLZeroReturnError
from typing import TYPE_CHECKING

import anyio
import anyio.abc
import anyio.streams.tls

from .events import Closed, Event, RawData, Updated
from .protocol import ProtocolWrapper
from .task_group import TaskGroup
from .typing import AppWrapper, ConnectionState, LifespanState
from .utils import build_tls_extension, parse_socket_addr
from .worker_context import AnyioSingleTask, WorkerContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from .config import Config

MAX_RECV = 2**16


class TCPServer:
    """Handles a single TCP connection, managing protocol negotiation and I/O."""

    def __init__(
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        state: LifespanState,
        stream: anyio.abc.SocketStream,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.protocol: ProtocolWrapper
        self.send_lock = anyio.Lock()
        self.idle_task = AnyioSingleTask()
        self.state = state
        self.stream = stream

        self._idle_handle: anyio.CancelScope | None = None

    async def run(self) -> None:
        """Run the server for this connection."""
        try:
            alpn_protocol = self.stream.extra(anyio.streams.tls.TLSAttribute.alpn_protocol)  # noqa: S610
            tls_extension = build_tls_extension(self.config, self.stream)
        except anyio.TypedAttributeLookupError:  # Not SSL
            alpn_protocol = "http/1.1"
            tls_extension = None

        try:
            socket = self.stream.extra(anyio.abc.SocketAttribute.raw_socket)  # noqa: S610
            client = parse_socket_addr(socket.family, socket.getpeername())
            server = parse_socket_addr(socket.family, socket.getsockname())

            async with TaskGroup() as task_group:
                self._task_group = task_group
                self.protocol = ProtocolWrapper(
                    self.app,
                    self.config,
                    self.context,
                    task_group,
                    ConnectionState(self.state.copy()),
                    client,
                    server,
                    self.protocol_send,
                    tls_extension,
                    alpn_protocol,
                )
                await self.protocol.initiate()
                await self.idle_task.restart(self._task_group, self._idle_timeout)
                await self._read_data()
        except OSError:
            pass
        finally:
            await self._close()

    async def protocol_send(self, event: Event) -> None:
        """Forward a protocol event to the underlying stream."""
        if isinstance(event, RawData):
            async with self.send_lock:
                try:
                    with anyio.CancelScope(shield=True):
                        await self.stream.send(event.data)
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, TimeoutError):
                    await self.protocol.handle(Closed())
        elif isinstance(event, Closed):
            await self._close()
            await self.protocol.handle(Closed())
        elif isinstance(event, Updated):
            if event.idle:
                await self.idle_task.restart(self._task_group, self._idle_timeout)
            else:
                await self.idle_task.stop()

    async def _read_data(self) -> None:
        while True:
            try:
                with anyio.fail_after(self.config.read_timeout or inf):
                    data = await self.stream.receive(MAX_RECV)
            except (  # noqa: PERF203
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
                anyio.EndOfStream,
                TimeoutError,
                SSLZeroReturnError,
            ):
                break
            else:
                await self.protocol.handle(RawData(data))
                if data == b"":
                    break
        await self.protocol.handle(Closed())

    async def _close(self) -> None:
        with contextlib.suppress(
            OSError,
            anyio.BrokenResourceError,
            AttributeError,
            NotImplementedError,
            TypeError,
            anyio.BusyResourceError,
            anyio.ClosedResourceError,
        ):
            # They're already gone, nothing to do, or it is a SSL stream
            await self.stream.send_eof()
        with contextlib.suppress(
            SSLError, anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.BusyResourceError
        ):
            await self.stream.aclose()

    async def _idle_timeout(self) -> None:
        with anyio.move_on_after(self.config.keep_alive_timeout):
            await self.context.terminated.wait()

        with anyio.CancelScope(shield=True):
            await self._initiate_server_close()

    async def _initiate_server_close(self) -> None:
        await self.protocol.handle(Closed())
        with contextlib.suppress(SSLError, anyio.BrokenResourceError, anyio.BusyResourceError):
            await self.stream.aclose()


def tcp_server_handler(
    app: AppWrapper,
    config: Config,
    context: WorkerContext,
    state: LifespanState,
) -> Callable:
    """Return a handler callable suitable for use with anyio's listener.serve()."""

    async def handler(stream: anyio.abc.SocketStream) -> None:
        tcp_server = TCPServer(app, config, context, state, stream)
        await tcp_server.run()

    return handler
