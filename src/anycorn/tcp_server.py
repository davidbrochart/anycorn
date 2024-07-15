from __future__ import annotations

from math import inf
from ssl import SSLError, SSLZeroReturnError

import anyio

from .config import Config
from .events import Closed, Event, RawData, Updated
from .protocol import ProtocolWrapper
from .task_group import TaskGroup
from .typing import AppWrapper, ConnectionState, LifespanState
from .utils import parse_socket_addr
from .worker_context import AnyioSingleTask, WorkerContext

MAX_RECV = 2**16


class TCPServer:
    def __init__(
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        state: LifespanState,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.protocol: ProtocolWrapper
        self.send_lock = anyio.Lock()
        self.idle_task = AnyioSingleTask()
        self.state = state

        self._idle_handle: anyio.CancelScope | None = None

    async def run(self, stream: anyio.abc.SocketStream) -> None:
        self.stream = stream
        try:
            alpn_protocol = self.stream.extra(anyio.streams.tls.TLSAttribute.alpn_protocol)
            ssl = True
        except anyio.TypedAttributeLookupError:  # Not SSL
            alpn_protocol = "http/1.1"
            ssl = False

        try:
            socket = self.stream.extra(anyio.abc.SocketAttribute.raw_socket)
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
                    ssl,
                    client,
                    server,
                    self.protocol_send,
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
        if isinstance(event, RawData):
            async with self.send_lock:
                try:
                    with anyio.CancelScope(shield=True):  # as cancel_scope:
                        # cancel_scope.shield = True
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
            except (
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
        try:
            await self.stream.send_eof()
        except (
            anyio.BrokenResourceError,
            AttributeError,
            NotImplementedError,
            TypeError,
            anyio.BusyResourceError,
            anyio.ClosedResourceError,
        ):
            # They're already gone, nothing to do
            # Or it is a SSL stream
            pass
        try:
            await self.stream.aclose()
        except (SSLError, anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    async def _idle_timeout(self) -> None:
        with anyio.move_on_after(self.config.keep_alive_timeout):
            await self.context.terminated.wait()

        with anyio.CancelScope(shield=True):
            await self._initiate_server_close()

    async def _initiate_server_close(self) -> None:
        await self.protocol.handle(Closed())
        try:
            await self.stream.aclose()
        except (SSLError, anyio.BrokenResourceError, anyio.BusyResourceError):
            pass
