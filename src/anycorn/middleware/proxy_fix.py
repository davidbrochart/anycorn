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

            if (
                self.mode == "modern"
                and (value := _get_trusted_value(b"forwarded", headers, self.trusted_hops))
                is not None
            ):
                # RFC 7239 permits optional whitespace around the ";" separators,
                # case-insensitive parameter names, and quoted values (e.g.
                # for="[2001:db8::1]:4711"). Normalise each part before matching, so a
                # header like "for=1.2.3.4; proto=https; host=example.com" is parsed
                # rather than having proto and host silently dropped.
                for part in value.split(";"):
                    param, _, param_value = part.strip().partition("=")
                    param_value = _unquote(param_value.strip())
                    param = param.lower()
                    if param == "for":
                        client = param_value
                    elif param == "host":
                        host = param_value
                    elif param == "proto":
                        scheme = param_value

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
