from __future__ import annotations

from collections.abc import Awaitable
from typing import Callable

from ..config import Config
from ..events import Event, RawData
from ..typing import AppWrapper, ConnectionState, TaskGroup, TLSExtension, WorkerContext
from .h2 import H2Protocol
from .h11 import H2CProtocolRequiredError, H2ProtocolAssumedError, H11Protocol


class ProtocolWrapper:
    def __init__(
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
        return await self.protocol.initiate()

    async def handle(self, event: Event) -> None:
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
