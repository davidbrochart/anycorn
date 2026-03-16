"""Tests for anycorn utility functions including header handling and TLS extensions."""

from __future__ import annotations

import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from anyio import TypedAttributeLookupError
from anyio.streams.tls import TLSAttribute

from anycorn.config import Config
from anycorn.utils import (
    build_and_validate_headers,
    build_tls_extension,
    default_tls_extension,
    filter_pseudo_headers,
    is_asgi,
    suppress_body,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from anycorn.typing import Scope


@pytest.mark.parametrize(
    ("method", "status", "expected"), [("HEAD", 200, True), ("GET", 200, False), ("GET", 101, True)]
)
def test_suppress_body(method: str, status: int, expected: bool) -> None:  # noqa: FBT001
    assert suppress_body(method, status) is expected


class ASGIClassInstance:
    """ASGI callable class instance for testing is_asgi detection."""

    def __init__(self) -> None:
        pass

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        pass


async def asgi_callable(scope: Scope, receive: Callable, send: Callable) -> None:
    pass


class WSGIClassInstance:
    """WSGI callable class instance for testing is_asgi detection."""

    def __init__(self) -> None:
        pass

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:  # noqa: ARG002
        return []


def wsgi_callable(environ: dict, start_response: Callable) -> Iterable[bytes]:  # noqa: ARG001
    return []


@pytest.mark.parametrize(
    ("app", "expected"),
    [
        (WSGIClassInstance(), False),
        (ASGIClassInstance(), True),
        (wsgi_callable, False),
        (asgi_callable, True),
    ],
)
def test_is_asgi(app: Any, expected: bool) -> None:  # noqa: ANN401, FBT001
    assert is_asgi(app) == expected


def test_build_and_validate_headers_validate() -> None:
    with pytest.raises(TypeError):
        build_and_validate_headers([("string", "string")])  # type: ignore[list-item]


def test_build_and_validate_headers_pseudo() -> None:
    with pytest.raises(ValueError, match="Pseudo headers are not valid"):
        build_and_validate_headers([(b":authority", b"quart")])


def test_filter_pseudo_headers() -> None:
    result = filter_pseudo_headers(
        [(b":authority", b"quart"), (b":path", b"/"), (b"user-agent", b"something")]
    )
    assert result == [(b"host", b"quart"), (b"user-agent", b"something")]


def test_filter_pseudo_headers_no_authority() -> None:
    result = filter_pseudo_headers(
        [(b"host", b"quart"), (b":path", b"/"), (b"user-agent", b"something")]
    )
    assert result == [(b"host", b"quart"), (b"user-agent", b"something")]


class _DummyStream:
    def extra(self, attr: object) -> object:
        raise TypedAttributeLookupError(attr)


def test_build_tls_extension_missing_tls_attributes() -> None:
    config = Config()
    extension = build_tls_extension(config, _DummyStream())  # type: ignore[arg-type]
    assert dict(extension) == default_tls_extension()


class _FakeStream:
    def __init__(self, extras: dict[Any, Any]) -> None:
        self._extras = extras

    def extra(self, attr: Any) -> Any:  # noqa: ANN401
        if attr in self._extras:
            return self._extras[attr]
        raise TypedAttributeLookupError(attr)


class _FakeSSLObject:
    def __init__(
        self,
        der_bytes: bytes | None,
        verify_mode: int = ssl.CERT_OPTIONAL,
        cipher_name: str = "TLS_AES_128_GCM_SHA256",
    ) -> None:
        self._der_bytes = der_bytes
        self._cipher_name = cipher_name
        self.context = SimpleNamespace(verify_mode=verify_mode)

    def get_verified_chain(self) -> tuple[bytes, ...]:
        if self._der_bytes:
            return (self._der_bytes,)
        return ()

    def get_unverified_chain(self) -> tuple[bytes, ...]:
        return self.get_verified_chain()

    def getpeercert(self, binary_form: bool = False) -> Any:  # noqa: ANN401, FBT001, FBT002
        if binary_form:
            return self._der_bytes
        if self._der_bytes:
            return {"subject": ((("commonName", "localhost"),),)}
        return {}

    def cipher(self) -> tuple[str, str, int]:
        return (self._cipher_name, "TLSv1.3", 128)


def test_build_tls_extension_with_client_certificate() -> None:
    pem_cert = Path("tests/assets/cert.pem").read_text()
    der_bytes = ssl.PEM_cert_to_DER_cert(pem_cert)
    fake_ssl = _FakeSSLObject(der_bytes, verify_mode=ssl.CERT_REQUIRED)
    stream = _FakeStream(
        {
            TLSAttribute.tls_version: "TLSv1.3",
            TLSAttribute.ssl_object: fake_ssl,
            TLSAttribute.peer_certificate: {"subject": ((("commonName", "localhost"),),)},
        }
    )
    extension = build_tls_extension(Config(), stream)  # type: ignore[arg-type]
    assert extension["tls_version"] == 0x0304  # noqa: PLR2004
    assert extension["client_cert_chain"]
    assert extension["client_cert_name"] == "CN=localhost"
    assert extension["cipher_suite"] == 0x1301  # noqa: PLR2004
    assert extension["client_cert_error"] is None


def test_build_tls_extension_missing_required_certificate() -> None:
    fake_ssl = _FakeSSLObject(None, verify_mode=ssl.CERT_REQUIRED)
    stream = _FakeStream(
        {
            TLSAttribute.tls_version: "TLSv1.2",
            TLSAttribute.ssl_object: fake_ssl,
        }
    )
    extension = build_tls_extension(Config(), stream)  # type: ignore[arg-type]
    assert extension["client_cert_chain"] == ()
    assert extension["client_cert_name"] is None
    assert extension["client_cert_error"] == "missing-client-certificate"
