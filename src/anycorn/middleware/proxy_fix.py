"""Middleware for extracting client information from reverse-proxy forwarding headers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from anycorn.typing import ASGIFramework, Scope


class ProxyFixMiddleware:
    """ASGI middleware that rewrites scope fields based on X-Forwarded-* or Forwarded headers."""

    def __init__(
        self,
        app: ASGIFramework,
        mode: Literal["legacy", "modern"] = "legacy",
        trusted_hops: int = 1,
    ) -> None:
        self.app = app
        self.mode = mode
        self.trusted_hops = trusted_hops

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        """Process the ASGI scope and apply proxy header rewrites before passing to the app."""
        # Keep the `or` instead of `in {'http' …}` to allow type narrowing
        if scope["type"] == "http" or scope["type"] == "websocket":
            # Shallow: only client, scheme and headers are replaced, and headers is
            # rebuilt as a new list rather than mutated. A deepcopy also copied
            # scope["state"], which carries whatever lifespan put there - a database
            # pool, a client, a lock - and raised TypeError on anything unpicklable.
            scope = scope.copy()
            headers = scope["headers"]
            client: str | None = None
            scheme: str | None = None
            host: str | None = None

            element = None
            if self.mode == "modern":
                element = _select_forwarded_element(headers, self.trusted_hops)

            if element is not None:
                client = element.get("for")
                host = element.get("host")
                scheme = element.get("proto")
            else:
                client = _get_trusted_value(b"x-forwarded-for", headers, self.trusted_hops)
                scheme = _get_trusted_value(b"x-forwarded-proto", headers, self.trusted_hops)
                host = _get_trusted_value(b"x-forwarded-host", headers, self.trusted_hops)

            if client is not None:
                scope["client"] = (client, 0)

            if scheme is not None:
                scope["scheme"] = scheme

            if host is not None:
                headers = [
                    (name, header_value)
                    for name, header_value in headers
                    if name.lower() != b"host"
                ]
                headers.append((b"host", host.encode()))
                scope["headers"] = headers

        await self.app(scope, receive, send)


def _split_outside_quotes(value: str, delimiter: str) -> list[str]:
    r"""Split *value* on *delimiter*, ignoring delimiters inside a quoted-string.

    RFC 7239 element (",") and pair (";") separators are structural only when they
    sit outside a quoted-string, so a value such as ``host="a;b,c"`` is a single
    pair carrying a single value rather than three fragments. Quote state is tracked
    with RFC 7230 backslash escapes (an escaped quote does not end the string), and
    the substrings are returned verbatim - quotes and escapes intact - for _unquote
    to decode.
    """
    parts: list[str] = []
    start = 0
    in_quotes = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and in_quotes:
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == delimiter and not in_quotes:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _parse_forwarded_element(element: str) -> dict[str, str]:
    """Parse one Forwarded element into a mapping of lower-cased param to value.

    A later duplicate of a param wins, matching how the last write of a repeated
    field would be read; malformed pairs with no "=" are skipped.
    """
    pairs: dict[str, str] = {}
    for pair in _split_outside_quotes(element, ";"):
        name, sep, value = pair.partition("=")
        if not sep:
            continue
        pairs[name.strip().lower()] = _unquote(value.strip())
    return pairs


def _select_forwarded_element(
    headers: Iterable[tuple[bytes, bytes]], trusted_hops: int
) -> dict[str, str] | None:
    """Return the trusted Forwarded element (counting *trusted_hops* from the right).

    All ``forwarded`` header fields are gathered and their comma-separated elements
    concatenated, as a single field split across lines would be. Returns None when
    there are fewer elements than trusted hops, so the caller falls back to the
    X-Forwarded-* headers exactly as before.
    """
    if trusted_hops == 0:
        return None

    elements: list[dict[str, str]] = []
    for name, value in headers:
        if name.lower() == b"forwarded":
            elements.extend(
                _parse_forwarded_element(element)
                for element in _split_outside_quotes(value.decode("latin1"), ",")
            )

    if len(elements) >= trusted_hops:
        return elements[-trusted_hops]
    return None


def _unquote(value: str) -> str:
    r"""Decode an RFC 7239 value: a bare token unchanged, a quoted-string unwrapped.

    RFC 7239 says a value is a token or an RFC 7230 quoted-string, and inside a
    quoted-string a backslash escapes the following character (so `\"` is a literal
    quote and `\\` a literal backslash). Only unwrap when the value is actually
    wrapped in a balanced pair of quotes; a bare token that happens to contain a
    quote is left alone.
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':  # noqa: PLR2004
        return value
    inner = value[1:-1]
    if "\\" not in inner:
        return inner
    unescaped: list[str] = []
    escaped = False
    for char in inner:
        if escaped:
            unescaped.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            unescaped.append(char)
    if escaped:
        # A lone trailing backslash (malformed) is kept as written.
        unescaped.append("\\")
    return "".join(unescaped)


def _get_trusted_value(
    name: bytes, headers: Iterable[tuple[bytes, bytes]], trusted_hops: int
) -> str | None:
    if trusted_hops == 0:
        return None

    values = []
    for header_name, header_value in headers:
        if header_name.lower() == name:
            values.extend([value.decode("latin1").strip() for value in header_value.split(b",")])

    if len(values) >= trusted_hops:
        return values[-trusted_hops]

    return None
