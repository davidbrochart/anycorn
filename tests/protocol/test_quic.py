"""Tests for the QUIC protocol handler's configuration."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioquic.h3.connection import H3_ALPN, ErrorCode
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from anycorn.config import Config
from anycorn.events import RawData
from anycorn.protocol.quic import QuicProtocol, _Connection
from anycorn.typing import ConnectionState

if TYPE_CHECKING:
    from pathlib import Path

_KEY_PASSWORD = "s3cret"  # noqa: S105  # test key encryption password, not a secret


def _write_encrypted_cert(directory: Path) -> tuple[str, str]:
    """Write a self-signed cert and a password-encrypted private key, return their paths."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))  # noqa: DTZ001
        .not_valid_after(datetime.datetime(2050, 1, 1))  # noqa: DTZ001
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(_KEY_PASSWORD.encode()),
        )
    )
    return str(certfile), str(keyfile)


def _make_protocol(
    config: Config, send: AsyncMock | None = None, context: MagicMock | None = None
) -> QuicProtocol:
    return QuicProtocol(
        MagicMock(),  # app
        config,
        MagicMock() if context is None else context,
        MagicMock(),  # task_group
        ConnectionState({}),
        ("192.0.2.1", 4433),  # server
        AsyncMock() if send is None else send,
    )


def test_loads_a_password_protected_http3_key(tmp_path: Path) -> None:
    """An encrypted HTTP/3 private key loads when keyfile_password is set.

    Without forwarding the password to aioquic's load_cert_chain, construction raises
    "Password was not given but private key is encrypted".

    https://github.com/pgjones/hypercorn/issues/84
    """
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile
    config.keyfile_password = _KEY_PASSWORD

    protocol = _make_protocol(config)

    assert protocol.quic_config.private_key is not None


def test_encrypted_http3_key_without_password_is_an_error(tmp_path: Path) -> None:
    """The same key without a password still fails, so the loader isn't silently lenient."""
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile

    with pytest.raises(TypeError, match="Password was not given"):
        _make_protocol(config)


@pytest.mark.anyio
async def test_close_all_closes_each_connection_once(tmp_path: Path) -> None:
    """Shutdown tells every peer once, with the close nginx sends on a graceful stop.

    The code matters: an application close carrying H3_NO_ERROR is what nginx
    finalizes an HTTP/3 connection with, and a peer reads a transport-level close or
    any other code as the connection having failed rather than been shut down.
    """
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile
    config.keyfile_password = _KEY_PASSWORD
    send = AsyncMock()
    protocol = _make_protocol(config, send)

    quic = MagicMock()
    quic.datagrams_to_send.return_value = [(b"close", ("192.0.2.1", 4433))]
    connection = _Connection(cids={b"one", b"two"}, quic=quic, task=MagicMock())
    # Registered under each of its connection ids, as handle() leaves it
    protocol.connections = {b"one": connection, b"two": connection}

    await protocol.close_all()

    quic.close.assert_called_once_with(error_code=ErrorCode.H3_NO_ERROR)
    # Once, not once per connection id
    send.assert_awaited_once()


# Only ever recorded as the peer a datagram came from: nothing is bound or sent to it,
# so the port neither has to be free nor to exist. Same goes for the addresses handed
# to _client_initial and _make_protocol. TEST-NET-1 (RFC 5737) is reserved for
# documentation and is not routable, so none of them can be mistaken for a real host.
CLIENT_ADDRESS = ("192.0.2.1", 44444)


def _client_initial() -> bytes:
    """Return a genuine QUIC Initial datagram, as a client opening a connection sends."""
    client = QuicConnection(configuration=QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN))
    client.connect(("192.0.2.1", 4433), now=0.0)
    datagrams = client.datagrams_to_send(now=0.0)
    return datagrams[0][0]


def _protocol_for_initial(config: Config, *, terminated: bool) -> QuicProtocol:
    context = MagicMock()
    context.time.return_value = 0.0
    context.terminated.is_set.return_value = terminated
    # Awaited by send_all once a connection exists, so it cannot be a plain MagicMock
    context.single_task_class.return_value = AsyncMock()
    return _make_protocol(config, context=context)


@pytest.mark.anyio
async def test_new_connection_refused_once_terminated(tmp_path: Path) -> None:
    """A worker that is shutting down must not take on a new QUIC connection.

    Paired with the test below, which feeds the very same datagram to a running
    worker: without it this would still pass if the Initial were simply unacceptable.
    """
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile
    config.keyfile_password = _KEY_PASSWORD
    protocol = _protocol_for_initial(config, terminated=True)

    await protocol.handle(RawData(data=_client_initial(), address=CLIENT_ADDRESS))

    assert protocol.connections == {}


@pytest.mark.anyio
async def test_new_connection_accepted_whilst_running(tmp_path: Path) -> None:
    """The same Initial is accepted by a worker that is still running."""
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile
    config.keyfile_password = _KEY_PASSWORD
    protocol = _protocol_for_initial(config, terminated=False)

    await protocol.handle(RawData(data=_client_initial(), address=CLIENT_ADDRESS))

    assert protocol.connections != {}


def _ended_quic() -> QuicConnection:
    """Return a connection aioquic has finished closing, and so has no timer left."""
    quic = QuicConnection(configuration=QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN))
    quic.connect(("192.0.2.1", 4433), now=0.0)
    quic.datagrams_to_send(now=0.0)
    quic.close()
    quic.datagrams_to_send(now=0.0)  # writes the close, beginning the closing period
    timer = quic.get_timer()
    assert timer is not None
    quic.handle_timer(now=timer)  # closing period over: the connection ends
    assert quic.get_timer() is None
    return quic


@pytest.mark.anyio
async def test_handle_timer_skips_a_connection_that_has_ended(tmp_path: Path) -> None:
    """A timer left over from before the connection ended must not be handled.

    aioquic drops its timer once a connection terminates, and handle_timer() then
    compares the time against it and raises TypeError. A CONNECTION_CLOSE arriving
    from the peer whilst a timer is pending is enough to reach this, so the timer
    has to check that the connection is still live rather than assume it.
    """
    certfile, keyfile = _write_encrypted_cert(tmp_path)
    config = Config()
    config.certfile = certfile
    config.keyfile = keyfile
    config.keyfile_password = _KEY_PASSWORD

    context = MagicMock()
    context.time.return_value = 0.0
    context.sleep = AsyncMock()
    send = AsyncMock()
    protocol = _make_protocol(config, send, context)
    connection = _Connection(cids=set(), quic=_ended_quic(), task=MagicMock())

    await protocol._handle_timer(0.0, connection)

    # Nothing was sent on behalf of a connection that is already over
    send.assert_not_awaited()
