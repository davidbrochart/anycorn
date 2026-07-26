"""Tests for the HTTP-to-HTTPS redirect middleware."""

from __future__ import annotations

import pytest

from anycorn.middleware import HTTPToHTTPSRedirectMiddleware
from anycorn.typing import ConnectionState, HTTPScope, WebsocketScope
from tests.helpers import empty_framework


@pytest.mark.anyio
@pytest.mark.parametrize("raw_path", [b"/abc", b"/abc%3C"])
async def test_http_to_https_redirect_middleware_http(raw_path: bytes) -> None:
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, "localhost")
    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    scope = HTTPScope(
        type="http",
        asgi={},
        http_version="2",
        method="GET",
        scheme="http",
        path=raw_path.decode(),
        raw_path=raw_path,
        query_string=b"a=b",
        root_path="",
        headers=[],
        client=("127.0.0.1", 80),
        server=None,
        extensions={},
        state=ConnectionState({}),
    )

    await app(scope, None, send)  # type: ignore[invalid-argument-type]

    assert sent_events == [
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", b"https://localhost%s?a=b" % raw_path)],
        },
        {"type": "http.response.body"},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("raw_path", [b"/abc", b"/abc%3C"])
async def test_http_to_https_redirect_middleware_websocket(raw_path: bytes) -> None:
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, "localhost")
    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    scope = WebsocketScope(
        type="websocket",
        asgi={},
        http_version="1.1",
        scheme="ws",
        path=raw_path.decode(),
        raw_path=raw_path,
        query_string=b"a=b",
        root_path="",
        headers=[],
        client=None,
        server=None,
        subprotocols=[],
        extensions={"websocket.http.response": {}},
        state=ConnectionState({}),
    )
    await app(scope, None, send)  # type: ignore[invalid-argument-type]

    assert sent_events == [
        {
            "type": "websocket.http.response.start",
            "status": 307,
            "headers": [(b"location", b"wss://localhost%s?a=b" % raw_path)],
        },
        {"type": "websocket.http.response.body"},
    ]


@pytest.mark.anyio
async def test_http_to_https_redirect_middleware_websocket_http2() -> None:
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, "localhost")
    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    scope = WebsocketScope(
        type="websocket",
        asgi={},
        http_version="2",
        scheme="ws",
        path="/abc",
        raw_path=b"/abc",
        query_string=b"a=b",
        root_path="",
        headers=[],
        client=None,
        server=None,
        subprotocols=[],
        extensions={"websocket.http.response": {}},
        state=ConnectionState({}),
    )
    await app(scope, None, send)  # type: ignore[invalid-argument-type]

    assert sent_events == [
        {
            "type": "websocket.http.response.start",
            "status": 307,
            "headers": [(b"location", b"https://localhost/abc?a=b")],
        },
        {"type": "websocket.http.response.body"},
    ]


@pytest.mark.anyio
async def test_http_to_https_redirect_middleware_websocket_no_rejection() -> None:
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, "localhost")
    sent_events = []

    async def send(message: dict) -> None:
        nonlocal sent_events
        sent_events.append(message)

    scope = WebsocketScope(
        type="websocket",
        asgi={},
        http_version="2",
        scheme="ws",
        path="/abc",
        raw_path=b"/abc",
        query_string=b"a=b",
        root_path="",
        headers=[],
        client=None,
        server=None,
        subprotocols=[],
        extensions={},
        state=ConnectionState({}),
    )
    await app(scope, None, send)  # type: ignore[invalid-argument-type]

    assert sent_events == [{"type": "websocket.close"}]


def test_http_to_https_redirect_new_url_header() -> None:
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, None)
    new_url = app._new_url(
        "https",
        HTTPScope(
            http_version="1.1",
            asgi={},
            method="GET",
            headers=[(b"host", b"localhost")],
            path="/",
            root_path="",
            query_string=b"",
            raw_path=b"/",
            scheme="http",
            type="http",
            client=None,
            server=None,
            extensions={},
            state=ConnectionState({}),
        ),
    )
    assert new_url == "https://localhost/"


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        (b"/x\xff", "https://localhost/x%FF"),  # not UTF-8 at all
        (b"/caf\xc3\xa9", "https://localhost/caf%C3%A9"),  # raw UTF-8
        (b"/caf%C3%A9", "https://localhost/caf%C3%A9"),  # already escaped: not again
        (b"/a%20b", "https://localhost/a%20b"),
        (b"/~u/x,y;z", "https://localhost/~u/x,y;z"),  # legal unescaped, left alone
    ],
)
def test_new_url_percent_escapes_a_target_that_is_not_a_uri(raw_path: bytes, expected: str) -> None:
    """raw_path is the bytes as received, and a Location header has to be a URI.

    Decoding those bytes as UTF-8 raised on a target that is not UTF-8 - which
    HTTP/2 and HTTP/3 hand over verbatim - so the request died here instead of
    being redirected.
    """
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, None)
    scope = _scope_with_host(b"localhost")
    scope["raw_path"] = raw_path

    assert app._new_url("https", scope) == expected


def _scope_with_host(host: bytes) -> HTTPScope:
    return HTTPScope(
        http_version="1.1",
        asgi={},
        method="GET",
        headers=[(b"host", host)],
        path="/",
        root_path="",
        query_string=b"",
        raw_path=b"/",
        scheme="http",
        type="http",
        client=None,
        server=None,
        extensions={},
        state=ConnectionState({}),
    )


@pytest.mark.parametrize(
    "host",
    [
        b"example.com/elsewhere",  # grafts a path onto the target
        b"example.com\\elsewhere",  # the same, as some clients normalise backslashes
        b"user@example.com",  # userinfo, so the real host is what follows the @
        b"example.com?x=y",
        b"example.com#fragment",
        b"example.com ",
        b"",
    ],
)
def test_new_url_refuses_a_host_header_that_is_not_a_bare_host(host: bytes) -> None:
    """The Host header is the client's to write, and it lands in the redirect target.

    Anything but a bare host[:port] there lets a client choose where this server
    sends it - a Host of "example.com/elsewhere" redirected to
    https://example.com/elsewhere rather than back to this site.
    """
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, None)

    assert app._new_url("https", _scope_with_host(host)) is None


@pytest.mark.parametrize(
    "host", [b"example.com", b"example.com:8443", b"127.0.0.1:80", b"[::1]:80"]
)
def test_new_url_accepts_a_bare_host(host: bytes) -> None:
    """A plain host, with or without a port, is still used as before."""
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, None)

    assert app._new_url("https", _scope_with_host(host)) == f"https://{host.decode()}/"


def test_new_url_prefers_the_configured_host() -> None:
    """A pinned host is what makes this immune, and it is not second-guessed."""
    app = HTTPToHTTPSRedirectMiddleware(empty_framework, "pinned.example")

    assert app._new_url("https", _scope_with_host(b"attacker.example")) == "https://pinned.example/"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("host", "status"),
    [(b"example.com", b"307"), (b"[::1]:8080", b"307"), (b"example.com/evil", b"400")],
)
async def test_redirect_answers_an_unusable_host_with_400(host: bytes, status: bytes) -> None:
    """A Host that cannot be redirected to is the client's mistake, so 400 not 500.

    Refusing by raising surfaced as a 500, which reads as the server having broken
    and puts a client-supplied header into the error log of every deployment behind
    a scanner.
    """
    sent_events = []

    async def send(message: dict) -> None:
        sent_events.append(message)

    app = HTTPToHTTPSRedirectMiddleware(empty_framework, None)
    await app(_scope_with_host(host), None, send)  # type: ignore[arg-type]

    assert b"%d" % sent_events[0]["status"] == status
