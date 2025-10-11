from __future__ import annotations

import functools
import inspect
import os
import re
import socket
import ssl
import sys
from collections.abc import Awaitable, Iterable
from enum import Enum
from importlib import import_module
from multiprocessing.synchronize import Event as EventType
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    cast,
)

from anyio import TypedAttributeLookupError
from anyio.abc import SocketStream
from anyio.streams.tls import TLSAttribute

from .app_wrappers import ASGIWrapper, WSGIWrapper
from .config import Config
from .typing import AppWrapper, ASGIFramework, Framework, TLSExtension, WSGIFramework

if TYPE_CHECKING:
    from .protocol.events import Request


class ShutdownError(Exception):
    pass


class NoAppError(Exception):
    pass


class LifespanTimeoutError(Exception):
    def __init__(self, stage: str) -> None:
        super().__init__(
            f"Timeout whilst awaiting {stage}. Your application may not support the ASGI Lifespan "
            f"protocol correctly, alternatively the {stage}_timeout configuration is incorrect."
        )


class LifespanFailureError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Lifespan failure in {stage}. '{message}'")


class UnexpectedMessageError(Exception):
    def __init__(self, state: Enum, message_type: str) -> None:
        super().__init__(f"Unexpected message type, {message_type} given the state {state}")


class FrameTooLargeError(Exception):
    pass


_CERT_PATTERN = re.compile(r"-----BEGIN CERTIFICATE-----\s.*?-----END CERTIFICATE-----", re.DOTALL)

_TLS_VERSION_PREFIX = "TLSv"

_TLS_VERSION_MAP = {
    "SSLv3": 0x0300,
    "TLSv1": 0x0301,
    "TLSv1.1": 0x0302,
    "TLSv1.2": 0x0303,
    "TLSv1.3": 0x0304,
}

_SERVER_CERT_CACHE: dict[str, str | None] = {}

RFC4514_ATTRIBUTE_NAMES = {
    "commonName": "CN",
    "countryName": "C",
    "localityName": "L",
    "stateOrProvinceName": "ST",
    "organizationName": "O",
    "organizationalUnitName": "OU",
    "emailAddress": "emailAddress",
    "serialNumber": "serialNumber",
    "streetAddress": "street",
    "postalCode": "postalCode",
    "domainComponent": "DC",
}

TLS_CIPHER_NAME_TO_CODE: dict[str, int] = {
    # TLS 1.3 identifiers (RFC 8446, Appendix B.4)
    "TLS_AES_128_GCM_SHA256": 0x1301,
    "TLS_AES_256_GCM_SHA384": 0x1302,
    "TLS_CHACHA20_POLY1305_SHA256": 0x1303,
    "TLS_AES_128_CCM_SHA256": 0x1304,
    "TLS_AES_128_CCM_8_SHA256": 0x1305,
    # Common TLS 1.2 suites (IANA TLS Cipher Suite Registry)
    "ECDHE-RSA-AES128-GCM-SHA256": 0xC02F,
    "ECDHE-RSA-AES256-GCM-SHA384": 0xC030,
    "ECDHE-ECDSA-AES128-GCM-SHA256": 0xC02B,
    "ECDHE-ECDSA-AES256-GCM-SHA384": 0xC02C,
    "ECDHE-RSA-CHACHA20-POLY1305": 0xCCA8,
    "ECDHE-ECDSA-CHACHA20-POLY1305": 0xCCA9,
    "DHE-RSA-AES128-GCM-SHA256": 0x009E,
    "DHE-RSA-AES256-GCM-SHA384": 0x009F,
    "AES128-GCM-SHA256": 0x009C,
    "AES256-GCM-SHA384": 0x009D,
    "ECDHE-RSA-AES128-SHA256": 0xC027,
    "ECDHE-RSA-AES256-SHA384": 0xC028,
    "ECDHE-ECDSA-AES128-SHA256": 0xC023,
    "ECDHE-ECDSA-AES256-SHA384": 0xC024,
}


def default_tls_extension() -> TLSExtension:
    return {
        "server_cert": None,
        "client_cert_chain": (),
        "client_cert_name": None,
        "client_cert_error": None,
        "tls_version": None,
        "cipher_suite": None,
    }


def tls_version_to_int(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        mapping = _TLS_VERSION_MAP.get(value)
        if mapping is not None:
            return mapping
        if value.startswith(_TLS_VERSION_PREFIX):
            version = value[len(_TLS_VERSION_PREFIX) :]
            if version == "1":
                return _TLS_VERSION_MAP["TLSv1"]
            try:
                major, _, minor = version.partition(".")
                if major == "1" and minor.isdigit():
                    return 0x0300 + int(minor) + 1
            except ValueError:
                return None
    return None


@functools.lru_cache(maxsize=16)
def _cached_server_cert(path: str) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _CERT_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0)


def get_server_certificate_pem(config: Config) -> str | None:
    if config.certfile is None:
        return None
    path = str(Path(config.certfile).resolve())
    cached = _SERVER_CERT_CACHE.get(path)
    if cached is not None:
        return cached
    cert = _cached_server_cert(path)
    _SERVER_CERT_CACHE[path] = cert
    return cert


def _extract_client_chain(ssl_object: ssl.SSLObject) -> tuple[str, ...]:
    chain_der: tuple[bytes, ...] | tuple[()] = ()
    for method_name in ("get_verified_chain", "get_unverified_chain"):
        method = getattr(ssl_object, method_name, None)
        if callable(method):
            try:
                data = method()
            except ssl.SSLError:
                continue
            else:
                if data:
                    chain_der = tuple(data)
                    break
    if not chain_der:
        try:
            peer_cert = ssl_object.getpeercert(binary_form=True)
        except ssl.SSLError:
            peer_cert = None
        if peer_cert:
            chain_der = (peer_cert,)

    return tuple(ssl.DER_cert_to_PEM_cert(cert) for cert in chain_der if cert)


def _escape_rfc4514_value(value: str) -> str:
    escaped = []
    for index, char in enumerate(value):
        if (
            char in {",", "+", '"', "\\", "<", ">", ";"}
            or (index == 0 and char in {"#", " "})
            or (index == len(value) - 1 and char == " ")
        ):
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def _subject_to_rfc4514(subject: Iterable[Iterable[tuple[str, str]]]) -> str | None:
    rdns: list[str] = []
    try:
        for rdn in reversed(tuple(subject)):
            attrs = []
            for key, value in rdn:
                name = RFC4514_ATTRIBUTE_NAMES.get(key, key)
                attrs.append(f"{name}={_escape_rfc4514_value(str(value))}")
            rdns.append("+".join(attrs))
    except Exception:
        return None
    return ",".join(rdns) if rdns else None


def build_tls_extension(config: Config, stream: SocketStream) -> TLSExtension:
    """Extract TLS parameters for the ASGI connection scope.

    Harvest information from AnyIO's TLS attributes and Python's ``ssl`` library:

    * TLS version / cipher IDs follow RFC 8446 (TLS 1.3) and the IANA registry.
    * Client certificate chains are returned as PEM per ASGI spec.
    * Distinguished names are rendered using RFC 4514 formatting.

    References:
      - ASGI TLS Extension: https://asgi.readthedocs.io/en/latest/specs/tls.html
      - RFC 8446: https://www.rfc-editor.org/rfc/rfc8446
      - IANA TLS Cipher Suite Registry: https://www.iana.org/assignments/tls-parameters/tls-parameters.xhtml#tls-parameters-4
      - RFC 4514 (LDAP DN string format): https://www.rfc-editor.org/rfc/rfc4514
    """
    extension = default_tls_extension()

    server_cert = get_server_certificate_pem(config)
    if server_cert is not None:
        extension["server_cert"] = server_cert

    try:
        tls_version_value = stream.extra(TLSAttribute.tls_version)
    except TypedAttributeLookupError:
        tls_version_value = None
    extension["tls_version"] = tls_version_to_int(tls_version_value)

    try:
        ssl_object = stream.extra(TLSAttribute.ssl_object)
    except (TypedAttributeLookupError, AttributeError):
        ssl_object = None

    if ssl_object is not None:
        client_chain = _extract_client_chain(ssl_object)
        extension["client_cert_chain"] = client_chain
        extension["cipher_suite"] = None
        extension["client_cert_error"] = None

        try:
            cipher = ssl_object.cipher()
        except (AttributeError, ssl.SSLError):
            cipher = None
        if cipher:
            extension["cipher_suite"] = TLS_CIPHER_NAME_TO_CODE.get(cipher[0])

        try:
            context = ssl_object.context
        except AttributeError:
            context = None
        else:
            if (
                not client_chain
                and extension["client_cert_name"] is None
                and getattr(context, "verify_mode", ssl.CERT_NONE) == ssl.CERT_REQUIRED
            ):
                extension["client_cert_error"] = "missing-client-certificate"
    else:
        extension["client_cert_chain"] = ()

    if extension["client_cert_name"] is None:
        try:
            peer_certificate = stream.extra(TLSAttribute.peer_certificate)
        except TypedAttributeLookupError:
            peer_certificate = None
        if peer_certificate and "subject" in peer_certificate:
            extension["client_cert_name"] = _subject_to_rfc4514(
                cast("Iterable[Iterable[tuple[str, str]]]", peer_certificate["subject"])
            )

    if extension["client_cert_name"] is None and isinstance(ssl_object, ssl.SSLObject):
        try:
            cert_dict = ssl_object.getpeercert()
        except (ssl.SSLError, ValueError):
            cert_dict = None
        if cert_dict and "subject" in cert_dict:
            extension["client_cert_name"] = _subject_to_rfc4514(
                cast("Iterable[Iterable[tuple[str, str]]]", cert_dict["subject"])
            )

    if extension["client_cert_chain"] and not extension["client_cert_name"]:
        extension["client_cert_name"] = None

    return extension


def suppress_body(method: str, status_code: int) -> bool:
    return method == "HEAD" or 100 <= status_code < 200 or status_code in {204, 304}


def build_and_validate_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    # Validates that the header name and value are bytes
    validated_headers: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if name[0] == b":"[0]:
            raise ValueError("Pseudo headers are not valid")
        validated_headers.append((bytes(name).strip(), bytes(value).strip()))
    return validated_headers


def filter_pseudo_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    filtered_headers: list[tuple[bytes, bytes]] = [(b"host", b"")]  # Placeholder
    authority = None
    host = b""
    for name, value in headers:
        if name == b":authority":  # h2 & h3 libraries validate this is present
            authority = value
        elif name == b"host":
            host = value
        elif name[0] != b":"[0]:
            filtered_headers.append((name, value))
    filtered_headers[0] = (b"host", authority if authority is not None else host)
    return filtered_headers


def load_application(path: str, wsgi_max_body_size: int) -> AppWrapper:
    mode: Literal["asgi", "wsgi"] | None = None
    if ":" not in path:
        module_name, app_name = path, "app"
    elif path.count(":") == 2:
        mode, module_name, app_name = path.split(":", 2)  # type: ignore[assignment]
        if mode not in {"asgi", "wsgi"}:
            raise ValueError("Invalid mode, must be 'asgi', or 'wsgi'")
    else:
        module_name, app_name = path.split(":", 1)

    module_path = Path(module_name).resolve()
    sys.path.insert(0, str(module_path.parent))
    if module_path.is_file():
        import_name = module_path.with_suffix("").name
    else:
        import_name = module_path.name
    try:
        module = import_module(import_name)
    except ModuleNotFoundError as error:
        if error.name == import_name:
            raise NoAppError(f"Cannot load application from '{path}', module not found.")
        else:
            raise
    try:
        app = eval(app_name, vars(module))
    except NameError:
        raise NoAppError(f"Cannot load application from '{path}', application not found.")
    else:
        return wrap_app(app, wsgi_max_body_size, mode)


def wrap_app(
    app: Framework, wsgi_max_body_size: int, mode: Literal["asgi", "wsgi"] | None
) -> AppWrapper:
    if mode is None:
        mode = "asgi" if is_asgi(app) else "wsgi"
    if mode == "asgi":
        return ASGIWrapper(cast(ASGIFramework, app))
    else:
        return WSGIWrapper(cast(WSGIFramework, app), wsgi_max_body_size)


def files_to_watch() -> dict[Path, float]:
    last_updates: dict[Path, float] = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        path = Path(filename)
        try:
            last_updates[Path(filename)] = path.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError):
            pass
    return last_updates


def check_for_updates(files: dict[Path, float]) -> bool:
    for path, last_mtime in files.items():
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return True
        else:
            if mtime > last_mtime:
                return True
            else:
                files[path] = mtime
    return False


async def raise_shutdown(shutdown_event: Callable[..., Awaitable]) -> None:
    await shutdown_event()
    raise ShutdownError()


async def check_multiprocess_shutdown_event(
    shutdown_event: EventType, sleep: Callable[[float], Awaitable[Any]]
) -> None:
    while True:
        if shutdown_event.is_set():
            return
        await sleep(0.1)


def write_pid_file(pid_path: str) -> None:
    with open(pid_path, "w") as file_:
        file_.write(f"{os.getpid()}")


def parse_socket_addr(family: int, address: tuple) -> tuple[str, int] | None:
    if family == socket.AF_INET:
        return address
    elif family == socket.AF_INET6:
        return (address[0], address[1])
    else:
        return None


def repr_socket_addr(family: int, address: tuple) -> str:
    if family == socket.AF_INET:
        return f"{address[0]}:{address[1]}"
    elif family == socket.AF_INET6:
        return f"[{address[0]}]:{address[1]}"
    elif family == socket.AF_UNIX:
        return f"unix:{address}"
    else:
        return f"{address}"


def valid_server_name(config: Config, request: Request) -> bool:
    if len(config.server_names) == 0:
        return True

    host = ""
    for name, value in request.headers:
        if name.lower() == b"host":
            host = value.decode()
            break
    return host in config.server_names


def is_asgi(app: Any) -> bool:
    if inspect.iscoroutinefunction(app):
        return True
    elif hasattr(app, "__call__"):
        return inspect.iscoroutinefunction(app.__call__)
    return False
