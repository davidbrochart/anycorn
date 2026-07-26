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
    from .datagram import DatagramSocket
    from .worker_context import WorkerContext

# Long enough for the closes to reach a peer that is still there, short enough that
# an unreachable one cannot delay the worker's exit.
CLOSE_TIMEOUT = 1.0


class UDPServer:
    """Handles UDP datagrams for QUIC protocol connections."""

    def __init__(
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        state: LifespanState,
        socket: DatagramSocket,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.socket = socket
        self.state = state
        # QUIC drives sends from the timer and stream tasks as well as from the read
        # loop, and anyio permits one writer to a socket at a time - concurrent sends
        # raise BusyResourceError rather than interleaving. Mirrors TCPServer.
        self.send_lock = anyio.Lock()

    async def run(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Run the UDP server, forwarding datagrams to the QUIC protocol handler."""
        from .protocol.quic import (  # noqa: PLC0415
            QuicProtocol,  # h3/Quic is an optional part of Anycorn
        )

        task_status.started()
        server = parse_socket_addr(self.socket.socket.family, self.socket.socket.getsockname())
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

            try:
                while not self.context.terminated.is_set() or not self.protocol.idle:
                    data, address = await self.socket.receive()
                    await self.protocol.handle(RawData(data=data, address=address))
            finally:
                # Shutdown cancels this task, and a closed UDP socket tells the peer
                # nothing, so the close has to be sent before unwinding gets any
                # further - shielded, or the sends are cancelled too. Bounded so a
                # peer that cannot be written to cannot hold the worker open.
                with anyio.move_on_after(CLOSE_TIMEOUT, shield=True):
                    await self.protocol.close_all()

    async def protocol_send(self, event: Event) -> None:
        """Forward a protocol event back to the UDP socket."""
        if isinstance(event, RawData):
            assert event.address is not None
            async with self.send_lock:
                await self.socket.sendto(event.data, event.address[0], event.address[1])
