"""ASGI app used by the --daemon integration test.

Reports whether *this* worker process is itself allowed to spawn a child
process - the concrete, observable difference Config.daemon actually makes.
Python's multiprocessing raises AssertionError from Process.start() when the
calling process is daemonic; run() sets that flag from Config.daemon rather
than hardcoding it.
"""

from __future__ import annotations

import multiprocessing
from typing import Any


def _grandchild() -> None:
    pass


async def app(_scope: Any, _receive: Any, send: Any) -> None:  # noqa: ANN401
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_grandchild)
    try:
        process.start()
        process.join()
    except AssertionError as error:
        body = str(error).encode()
    else:
        body = b"ok"

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
