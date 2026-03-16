"""WebSocket echo server for Autobahn compliance testing."""

from __future__ import annotations

from typing import Any


async def app(_scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Handle WebSocket connections by echoing received messages."""
    while True:
        event = await receive()
        if event["type"] == "websocket.disconnect":
            break
        if event["type"] == "websocket.connect":
            await send({"type": "websocket.accept"})
        elif event["type"] == "websocket.receive":
            await send(
                {
                    "type": "websocket.send",
                    "bytes": event["bytes"],
                    "text": event["text"],
                }
            )
        elif event["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            break
