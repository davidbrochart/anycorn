"""QUIC protocol handler that manages multiple HTTP/3 connections."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from aioquic.buffer import Buffer
from aioquic.h3.connection import H3_ALPN, ErrorCode
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    ConnectionIdIssued,
    ConnectionIdRetired,
    ConnectionTerminated,
    ProtocolNegotiated,
)
from aioquic.quic.packet import (
    PACKET_TYPE_INITIAL,
    encode_quic_version_negotiation,
    pull_quic_header,
)

from anycorn.events import Closed, Event, RawData
from anycorn.typing import (
    AppWrapper,
    ConnectionState,
    SingleTask,
    TaskGroup,
    TLSExtension,
    WorkerContext,
)
from anycorn.utils import get_server_certificate_pem, tls_version_to_int

from .h3 import H3Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anycorn.config import Config

MIN_INITIAL_DATAGRAM_SIZE = 1200


@dataclass
class _Connection:
    cids: set[bytes]
    quic: QuicConnection
    task: SingleTask
    h3: H3Protocol | None = None


class QuicProtocol:
    """Manages QUIC connections and dispatches events to H3Protocol handlers."""

    def __init__(  # noqa: PLR0913
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        task_group: TaskGroup,
        state: ConnectionState,
        server: tuple[str, int] | None,
        send: Callable[[Event], Awaitable[None]],
    ) -> None:
        """Initialize the QUIC protocol handler."""
        self.app = app
        self.config = config
        self.context = context
        self.connections: dict[bytes, _Connection] = {}
        self.send = send
        self.server = server
        self.task_group = task_group
        self.state = state
        self._server_cert_pem = get_server_certificate_pem(config)

        self.quic_config = QuicConfiguration(alpn_protocols=H3_ALPN, is_client=False)
        assert config.certfile is not None
        assert config.keyfile is not None
        # Pass the key password through, as create_ssl_context does for TLS: without
        # it an encrypted HTTP/3 private key fails to load with "Password was not given
        # but private key is encrypted" (https://github.com/pgjones/hypercorn/issues/84).
        self.quic_config.load_cert_chain(
            certfile=pathlib.Path(config.certfile),
            keyfile=pathlib.Path(config.keyfile),
            password=config.keyfile_password,
        )

    @property
    def idle(self) -> bool:
        """Return True when there are no active QUIC connections."""
        return len(self.connections) == 0

    async def handle(self, event: Event) -> None:
        """Handle an incoming connection event."""
        if isinstance(event, RawData):
            try:
                header = pull_quic_header(Buffer(data=event.data), host_cid_length=8)
            except ValueError:
                return
            if (
                header.version is not None
                and header.version not in self.quic_config.supported_versions
            ):
                data = encode_quic_version_negotiation(
                    source_cid=header.destination_cid,
                    destination_cid=header.source_cid,
                    supported_versions=self.quic_config.supported_versions,
                )
                await self.send(RawData(data=data, address=event.address))
                return

            connection = self.connections.get(header.destination_cid)
            if (
                connection is None
                and len(event.data) >= MIN_INITIAL_DATAGRAM_SIZE
                and header.packet_type == PACKET_TYPE_INITIAL
                and not self.context.terminated.is_set()
            ):
                quic_connection = QuicConnection(
                    configuration=self.quic_config,
                    original_destination_connection_id=header.destination_cid,
                )
                connection = _Connection(
                    cids={header.destination_cid, quic_connection.host_cid},
                    quic=quic_connection,
                    task=self.context.single_task_class(),
                )
                self.connections[header.destination_cid] = connection
                self.connections[quic_connection.host_cid] = connection

            if connection is not None:
                connection.quic.receive_datagram(event.data, event.address, now=self.context.time())
                await self._handle_events(connection, event.address)
        elif isinstance(event, Closed):
            pass

    async def _flush_datagrams(self, connection: _Connection) -> None:
        """Write whatever the connection has queued out to the socket."""
        for data, address in connection.quic.datagrams_to_send(now=self.context.time()):
            await self.send(RawData(data=data, address=address))

    async def close_all(self) -> None:
        """Tell every peer the connection is going away, as nginx does on shutdown.

        Closing a UDP socket sends the peer nothing, so without this a client of a
        server that has stopped simply waits out its idle timeout rather than being
        told. nginx's `ngx_quic_close_connection()` sends CONNECTION_CLOSE, so do the
        same: an application-level close (frame 0x1d, what `frame_type=None` selects)
        carrying H3_NO_ERROR, matching the NGX_HTTP_V3_ERR_NO_ERROR nginx finalizes a
        graceful HTTP/3 shutdown with.

        Only the frame is sent. nginx then holds the connection open for 3*PTO to
        re-send it to any straggling packet, which is of no use here because the
        socket is closed immediately after the worker stops.
        """
        # One connection is registered under each of its connection ids, so walk the
        # distinct connections rather than sending a close per id. _Connection is an
        # unhashable dataclass, hence keying on identity rather than using a set.
        for connection in {id(entry): entry for entry in self.connections.values()}.values():
            connection.quic.close(error_code=ErrorCode.H3_NO_ERROR)
            await self._flush_datagrams(connection)

    async def send_all(self, connection: _Connection) -> None:
        """Send all pending datagrams and reschedule the connection timer."""
        await self._flush_datagrams(connection)

        timer = connection.quic.get_timer()
        if timer is not None:
            await connection.task.restart(
                self.task_group, partial(self._handle_timer, timer, connection)
            )

    async def _handle_events(  # noqa: C901
        self, connection: _Connection, client: tuple[str, int] | None = None
    ) -> None:
        event = connection.quic.next_event()
        while event is not None:
            if isinstance(event, ConnectionTerminated):
                await connection.task.stop()
                for cid in connection.cids:
                    del self.connections[cid]
                connection.cids = set()
            elif isinstance(event, ProtocolNegotiated):
                tls_extension = TLSExtension()
                if self._server_cert_pem is not None:
                    tls_extension["server_cert"] = self._server_cert_pem
                tls_context = getattr(connection.quic, "tls", None)
                if tls_context is not None:
                    tls_version = tls_version_to_int(getattr(tls_context, "version", None))
                    if tls_version is not None:
                        tls_extension["tls_version"] = tls_version
                    cipher_suite = getattr(tls_context, "cipher_suite", None)
                    if isinstance(cipher_suite, int):
                        tls_extension["cipher_suite"] = cipher_suite
                connection.h3 = H3Protocol(
                    self.app,
                    self.config,
                    self.context,
                    self.task_group,
                    # Copied per connection, as TCPServer does. One UDP socket carries
                    # every QUIC connection it is sent, so sharing the worker's state
                    # would hand one client's namespace to the next
                    ConnectionState(self.state.copy()),
                    tls_extension,
                    client,
                    self.server,
                    connection.quic,
                    partial(self.send_all, connection),
                )
            elif isinstance(event, ConnectionIdIssued):
                connection.cids.add(event.connection_id)
                self.connections[event.connection_id] = connection
            elif isinstance(event, ConnectionIdRetired):
                connection.cids.remove(event.connection_id)
                del self.connections[event.connection_id]

            if connection.h3 is not None:
                await connection.h3.handle(event)

            event = connection.quic.next_event()

        await self.send_all(connection)

    async def _handle_timer(self, timer: float, connection: _Connection) -> None:
        wait = max(0, timer - self.context.time())
        await self.context.sleep(wait)
        # The connection can end whilst this waits - a CONNECTION_CLOSE from the peer
        # ends it at once - and aioquic drops its timer when it does. get_timer() is
        # then None, and handle_timer() compares the time against it and raises
        # TypeError. Whoever ended the connection drains its events, so there is
        # nothing left here to do.
        if connection.quic.get_timer() is None:
            return
        connection.quic.handle_timer(now=self.context.time())
        await self._handle_events(connection, None)
