"""Middleware that redirects HTTP and WS requests to HTTPS and WSS respectively."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlunsplit

if TYPE_CHECKING:
    from collections.abc import Callable

    from anycorn.typing import ASGIFramework, HTTPScope, Scope, WebsocketScope, WWWScope


class HTTPToHTTPSRedirectMiddleware:
    """ASGI middleware that issues 307 redirects from HTTP/WS to HTTPS/WSS."""

    def __init__(self, app: ASGIFramework, host: str | None) -> None:
        self.app = app
        self.host = host

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        """Handle the ASGI call, redirecting insecure connections to their secure equivalents."""
        if scope["type"] == "http" and scope["scheme"] == "http":
            await self._send_http_redirect(scope, send)
        elif scope["type"] == "websocket" and scope["scheme"] == "ws":
            # If the server supports the WebSocket Denial Response
            # extension we can send a redirection response, if not we
            # can only deny the WebSocket connection.
            if "websocket.http.response" in scope.get("extensions", {}):
                await self._send_websocket_redirect(scope, send)
            else:
                await send({"type": "websocket.close"})
        else:
            return await self.app(scope, receive, send)
        return None

    async def _send_http_redirect(self, scope: HTTPScope, send: Callable) -> None:
        new_url = self._new_url("https", scope)
        await send(
            {
                "type": "http.response.start",
                "status": 307,
                "headers": [(b"location", new_url.encode())],
            }
        )
        await send({"type": "http.response.body"})

    async def _send_websocket_redirect(self, scope: WebsocketScope, send: Callable) -> None:
        # If the HTTP version is 2 we should redirect with a https
        # scheme not wss.
        scheme = "wss"
        if scope.get("http_version", "1.1") == "2":
            scheme = "https"

        new_url = self._new_url(scheme, scope)
        await send(
            {
                "type": "websocket.http.response.start",
                "status": 307,
                "headers": [(b"location", new_url.encode())],
            }
        )
        await send({"type": "websocket.http.response.body"})

    def _new_url(self, scheme: str, scope: WWWScope) -> str:
        host = self.host
        if host is None:
            for key, value in scope["headers"]:
                if key == b"host":
                    host = value.decode("latin-1")
                    break
        if host is None:
            msg = "Host to redirect to cannot be determined"
            raise ValueError(msg)

        path = scope.get("root_path", "") + scope["raw_path"].decode()
        return urlunsplit((scheme, host, path, scope["query_string"].decode(), ""))
