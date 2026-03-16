"""UDP server implementation for QUIC/HTTP3 connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import anyio.abc

from .events import Event, RawData
from .task_group import TaskGroup
from .typing import AppWrapper, ConnectionState, LifespanState
from .utils import parse_socket_addr

if TYPE_CHECKING:
    from .config import Config
    from .worker_context import WorkerContext


class UDPServer:
    """Handles UDP datagrams for QUIC protocol connections."""

    def __init__(
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        state: LifespanState,
        socket: anyio.abc.UDPSocket,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.socket = socket
        self.state = state

    async def run(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Run the UDP server, forwarding datagrams to the QUIC protocol handler."""
        from .protocol.quic import (  # noqa: PLC0415
            QuicProtocol,  # h3/Quic is an optional part of Anycorn
        )

        task_status.started()
        server = parse_socket_addr(
            self.socket.extra(anyio.abc.SocketAttribute.raw_socket).family,  # noqa: S610
            self.socket.extra(anyio.abc.SocketAttribute.raw_socket).getsockname(),  # noqa: S610
        )
        async with TaskGroup() as task_group:
            self.protocol = QuicProtocol(
                self.app,
                self.config,
                self.context,
                task_group,
                ConnectionState(self.state.copy()),
                server,
                self.protocol_send,
            )

            while not self.context.terminated.is_set() or not self.protocol.idle:
                data, address = await self.socket.receive()
                await self.protocol.handle(RawData(data=data, address=address))

    async def protocol_send(self, event: Event) -> None:
        """Forward a protocol event back to the UDP socket."""
        if isinstance(event, RawData):
            assert event.address is not None
            await self.socket.sendto(event.data, event.address[0], event.address[1])
