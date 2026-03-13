"""Protocol wrapper that selects and manages the appropriate HTTP protocol handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anycorn.events import Event, RawData

from .h2 import H2Protocol
from .h11 import H2CProtocolRequiredError, H2ProtocolAssumedError, H11Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anycorn.config import Config
    from anycorn.typing import AppWrapper, ConnectionState, TaskGroup, TLSExtension, WorkerContext


class ProtocolWrapper:
    """Wraps H11 and H2 protocols, upgrading as needed based on ALPN or upgrade headers."""

    def __init__(  # noqa: PLR0913
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        task_group: TaskGroup,
        state: ConnectionState,
        client: tuple[str, int] | None,
        server: tuple[str, int] | None,
        send: Callable[[Event], Awaitable[None]],
        tls: TLSExtension | None,
        alpn_protocol: str | None = None,
    ) -> None:
        """Initialize the protocol wrapper."""
        self.app = app
        self.config = config
        self.context = context
        self.task_group = task_group
        self.client = client
        self.server = server
        self.send = send
        self.state = state
        self.tls = tls
        self.protocol: H11Protocol | H2Protocol
        if alpn_protocol == "h2":
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.state,
                self.client,
                self.server,
                self.send,
                self.tls,
            )
        else:
            self.protocol = H11Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.state,
                self.client,
                self.server,
                self.send,
                self.tls,
            )

    async def initiate(self) -> None:
        """Initiate the underlying protocol."""
        return await self.protocol.initiate()

    async def handle(self, event: Event) -> None:
        """Handle an incoming event, upgrading the protocol if required."""
        try:
            return await self.protocol.handle(event)
        except H2ProtocolAssumedError as error:
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.state,
                self.client,
                self.server,
                self.send,
                self.tls,
            )
            await self.protocol.initiate()
            if error.data != b"":
                return await self.protocol.handle(RawData(data=error.data))
        except H2CProtocolRequiredError as error:
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.state,
                self.client,
                self.server,
                self.send,
                self.tls,
            )
            await self.protocol.initiate(error.headers, error.settings)
            if error.data != b"":
                return await self.protocol.handle(RawData(data=error.data))
