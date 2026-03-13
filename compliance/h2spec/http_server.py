"""HTTP/2 server for h2spec compliance testing."""

from __future__ import annotations

from typing import Any


async def app(_scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Handle HTTP requests and lifespan events for h2spec testing."""
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            break
        if event["type"] == "http.request" and not event.get("more_body", False):
            await _send_data(send)
            break
        if event["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            break


async def _send_data(send: Any) -> None:  # noqa: ANN401
    """Send an HTTP response with a simple 'Hello' body."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"5")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"Hello",
            "more_body": False,
        }
    )
