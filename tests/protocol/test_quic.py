"""Tests for the QUIC protocol handler's configuration."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from anycorn.config import Config
from anycorn.protocol.quic import QuicProtocol
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


def _make_protocol(config: Config) -> QuicProtocol:
    return QuicProtocol(
        MagicMock(),  # app
        config,
        MagicMock(),  # context
        MagicMock(),  # task_group
        ConnectionState({}),
        ("127.0.0.1", 4433),  # server
        AsyncMock(),  # send
    )


def test_loads_a_password_protected_http3_key(tmp_path: Path) -> None:
    """An encrypted HTTP/3 private key loads when keyfile_password is set (hypercorn #84).

    Without forwarding the password to aioquic's load_cert_chain, construction raises
    "Password was not given but private key is encrypted".
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
