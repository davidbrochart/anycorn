"""Integration test for WSGIWrapper.run_app deferring http.response.start.

Per PEP 3333, a WSGI app calls start_response(status, headers) to set the
response status and headers before producing any body output. For a
generator-based app - the idiomatic way to stream a WSGI response - that call
happens on first iteration, not on construction: calling a generator function
only builds the generator object, it never runs any of the function's body,
including its start_response() call. A server that checked whether
start_response had been called immediately after constructing that generator,
rather than after actually requesting its first chunk, would conclude every
such app "did not call start_response" and fail every single request to it -
despite the app doing nothing wrong.

This drives a real, running anycorn server (in-process, not a subprocess:
nothing here needs real OS-level process semantics) with a real HTTP client
against a generator-based WSGI app, so what's exercised is the whole
pipeline - the to_thread/from_thread bridge into the synchronous WSGI app and
the real ASGI message flow - rather than WSGIWrapper.run_app called directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import httpx2
import pytest

import anycorn
from anycorn.config import Config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def generator_app(_environ: dict, start_response: Callable) -> Iterator[bytes]:
    # A generator function's body - including this start_response() call - does
    # not run until the generator is first iterated, not when it is constructed.
    start_response("200 OK", [("Content-Type", "text/plain")])
    yield b"hello "
    yield b"world"


@pytest.mark.anyio
async def test_generator_wsgi_app_is_served_successfully(free_tcp_port: int) -> None:
    """A generator-based WSGI app must serve a real request end to end."""
    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = "-"
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()
        await tg.start(
            lambda *, task_status: anycorn.serve(
                generator_app,
                config,
                mode="wsgi",
                shutdown_trigger=shutdown.wait,
                task_status=task_status,
            )
        )
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{free_tcp_port}") as client:
            with anyio.fail_after(10):
                response = await client.get("/")
        shutdown.set()

    assert response.status_code == 200  # noqa: PLR2004
    assert response.headers["content-type"] == "text/plain"
    assert response.text == "hello world"
