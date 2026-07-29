"""Tests for the proxy fix middleware."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from anycorn.middleware import ProxyFixMiddleware
from anycorn.middleware.proxy_fix import _split_outside_quotes, _unquote
from anycorn.typing import ConnectionState, HTTPScope

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.anyio
async def test_proxy_fix_legacy() -> None:
    mock = AsyncMock()
    app = ProxyFixMiddleware(mock)
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "2",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-forwarded-for", b"127.0.0.1"),
            (b"x-forwarded-for", b"127.0.0.2"),
            (b"x-forwarded-proto", b"http,https"),
            (b"x-forwarded-host", b"example.com"),
        ],
        "client": ("127.0.0.3", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    await app(scope, None, None)  # type: ignore[invalid-argument-type]
    mock.assert_called()
    scope = mock.call_args[0][0]
    assert scope["client"] == ("127.0.0.2", 0)
    assert scope["scheme"] == "https"
    host_headers = [h for h in scope["headers"] if h[0].lower() == b"host"]
    assert host_headers == [(b"host", b"example.com")]


@pytest.mark.anyio
async def test_proxy_fix_modern() -> None:
    mock = AsyncMock()
    app = ProxyFixMiddleware(mock, mode="modern")
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "2",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"forwarded", b"for=127.0.0.1;proto=http,for=127.0.0.2;proto=https;host=example.com"),
        ],
        "client": ("127.0.0.3", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    await app(scope, None, None)  # type: ignore[invalid-argument-type]
    mock.assert_called()
    scope = mock.call_args[0][0]
    assert scope["client"] == ("127.0.0.2", 0)
    assert scope["scheme"] == "https"
    host_headers = [h for h in scope["headers"] if h[0].lower() == b"host"]
    assert host_headers == [(b"host", b"example.com")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.60", "192.0.2.60"),  # bare token, unchanged
        ('"192.0.2.60"', "192.0.2.60"),  # quoted-string, unwrapped
        ('"[2001:db8::1]:4711"', "[2001:db8::1]:4711"),  # quoted IPv6
        (r'"a\"b"', 'a"b'),  # quoted-pair: escaped quote
        (r'"a\\b"', r"a\b"),  # quoted-pair: escaped backslash
        ('""', ""),  # empty quoted-string
        ('"', '"'),  # single quote is not a balanced pair
        ('a"b', 'a"b'),  # stray quote in a token is left alone
    ],
)
def test_unquote(value: str, expected: str) -> None:
    assert _unquote(value) == expected


@pytest.mark.parametrize(
    ("value", "delimiter", "expected"),
    [
        ("a,b,c", ",", ["a", "b", "c"]),
        ('for="a,b",for=c', ",", ['for="a,b"', "for=c"]),  # comma inside quotes
        ('for=a;host="x;y";proto=z', ";", ["for=a", 'host="x;y"', "proto=z"]),
        (r'for="a\"b,c"', ",", [r'for="a\"b,c"']),  # escaped quote keeps quotes open
        ("", ",", [""]),
    ],
)
def test_split_outside_quotes(value: str, delimiter: str, expected: list[str]) -> None:
    assert _split_outside_quotes(value, delimiter) == expected


@pytest.mark.anyio
async def test_proxy_fix_modern_with_optional_whitespace() -> None:
    """RFC 7239 permits OWS after ";" and quoted values; both must still parse.

    A header like "for=127.0.0.2; proto=https; host=example.com" previously left
    proto and host unmatched (the parts began with a space), so scheme and host
    were silently not rewritten.
    """
    mock = AsyncMock()
    app = ProxyFixMiddleware(mock, mode="modern")
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "2",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"forwarded", b'for="127.0.0.2"; proto=https; host=example.com'),
        ],
        "client": ("127.0.0.3", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
    await app(scope, None, None)  # type: ignore[invalid-argument-type]
    mock.assert_called()
    scope = mock.call_args[0][0]
    assert scope["client"] == ("127.0.0.2", 0)
    assert scope["scheme"] == "https"
    host_headers = [h for h in scope["headers"] if h[0].lower() == b"host"]
    assert host_headers == [(b"host", b"example.com")]


def _http_scope(headers: list[tuple[bytes, bytes]]) -> HTTPScope:
    return {
        "type": "http",
        "asgi": {},
        "http_version": "2",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.3", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }


@pytest.mark.anyio
async def test_proxy_fix_modern_quoted_delimiters_are_not_split() -> None:
    """A "," or ";" inside a quoted value is data, not a separator."""
    mock = AsyncMock()
    app = ProxyFixMiddleware(mock, mode="modern")
    scope = _http_scope(
        [(b"forwarded", b'for="[2001:db8::1]:8080"; host="a;b,c"; proto=https')]
    )
    await app(scope, None, None)  # type: ignore[invalid-argument-type]
    scope = mock.call_args[0][0]
    assert scope["client"] == ("[2001:db8::1]:8080", 0)
    assert scope["scheme"] == "https"
    host_headers = [h for h in scope["headers"] if h[0].lower() == b"host"]
    assert host_headers == [(b"host", b"a;b,c")]


@pytest.mark.anyio
async def test_proxy_fix_modern_quoted_comma_does_not_shift_hops() -> None:
    """A comma inside a quoted value must not be counted as an extra element.

    With trusted_hops=2 and two real elements, the trusted element is the first.
    A naive comma split would see three fragments and pick the wrong one.
    """
    mock = AsyncMock()
    app = ProxyFixMiddleware(mock, mode="modern", trusted_hops=2)
    scope = _http_scope(
        [(b"forwarded", b'for="a,b"; proto=http, for=2.2.2.2; proto=https')]
    )
    await app(scope, None, None)  # type: ignore[invalid-argument-type]
    scope = mock.call_args[0][0]
    assert scope["client"] == ("a,b", 0)
    assert scope["scheme"] == "http"


@pytest.mark.anyio
async def test_proxy_fix_keeps_unpicklable_state() -> None:
    """The scope carries whatever lifespan put in state, which need not be copyable.

    A deepcopy of the whole scope raised TypeError on anything unpicklable - a
    database pool, a client, a lock - so every request through this middleware
    failed. Only client, scheme and headers are rewritten, so a shallow copy does.
    """
    lock = threading.Lock()
    state = {"pool": lock}
    seen: list[Any] = []

    async def app(scope: Any, _receive: Callable, _send: Callable) -> None:  # noqa: ANN401
        seen.append(scope)

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"127.0.0.1")],
        "client": ("localhost", 80),
        "scheme": "http",
        "state": state,
    }

    await ProxyFixMiddleware(app, mode="legacy").__call__(scope, AsyncMock(), AsyncMock())  # type: ignore[arg-type]

    # Handed on, not copied: the app sees the very object lifespan created
    assert seen[0]["state"]["pool"] is lock
    # And the caller's scope was left alone
    assert scope["client"] == ("localhost", 80)
    assert seen[0]["client"] == ("127.0.0.1", 0)
