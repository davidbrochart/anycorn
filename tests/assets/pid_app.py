"""ASGI app used by the SIGHUP integration test: reports this worker's PID."""

from __future__ import annotations

import os
from typing import Any


async def app(_scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    body = str(os.getpid()).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
