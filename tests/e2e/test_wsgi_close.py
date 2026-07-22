"""Integration test for the WSGI response_body.close() cleanup contract.

Per PEP 3333, once the server is done consuming the iterable a WSGI app
returns, it must call close() on it if the iterable has one - the app's way
of releasing resources tied to the response's lifetime (a DB connection, a
file handle). This drives a real, running anycorn server (in-process, not a
subprocess: nothing here needs real OS-level process semantics) with a real
HTTP client, so what is observed is the whole pipeline - the async request
handling, the to_thread/from_thread bridge into the synchronous WSGI app,
and the response object's cleanup - working together, not WSGIWrapper in
isolation.

The response body is deliberately a plain object with its own close()
method, not a generator relying on a try/finally. A generator's close() (and
so its finally block) gets called automatically by CPython's refcounting GC
the moment it becomes unreferenced, regardless of whether the server ever
calls it - which would make this test pass even if run_app's own
getattr(response_body, "close", None) call were deleted entirely. A plain
object has no such implicit GC hook, so only that explicit call can produce
the "closed" event asserted below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest

import anycorn
from anycorn.config import Config

if TYPE_CHECKING:
    from collections.abc import Callable


class _TrackedBody:
    """A WSGI response iterable with an explicit close(), as PEP 3333 expects."""

    def __init__(self, chunks: list[bytes], events: list[str], *, boom: bool = False) -> None:
        self._chunks = iter(chunks)
        self._events = events
        self._boom = boom

    def __iter__(self) -> _TrackedBody:
        return self

    def __next__(self) -> bytes:
        chunk = next(self._chunks)
        if self._boom and chunk == b"world":
            msg = "boom"
            raise RuntimeError(msg)
        return chunk

    def close(self) -> None:
        self._events.append("closed")


def _make_app(events: list[str], *, boom: bool = False) -> Callable:
    def app(_environ: dict, start_response: Callable) -> Any:  # noqa: ANN401
        start_response("200 OK", [("Content-Type", "text/plain")])
        events.append("opened")
        return _TrackedBody([b"hello ", b"world"], events, boom=boom)

    return app


@pytest.mark.anyio
async def test_wsgi_body_close_runs_after_normal_completion(free_tcp_port: int) -> None:
    events: list[str] = []
    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = "-"
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()
        await tg.start(
            lambda *, task_status: anycorn.serve(
                _make_app(events),
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
    assert response.text == "hello world"
    # "closed" only after "opened", and only once the whole body was read - not the
    # instant start_response() ran, which is all a broken server could still fake
    assert events == ["opened", "closed"]


@pytest.mark.anyio
async def test_wsgi_body_close_runs_after_error_mid_stream(free_tcp_port: int) -> None:
    """close() must run even when the app blows up partway through its own body.

    The client sees a broken response either way - the first chunk, and the
    headers committing to a 200, are already on the wire by the time the app
    raises. What matters here is that the response object's own cleanup still
    ran, despite the failure happening on the other side of the ASGI/WSGI
    boundary and a thread-pool hop.
    """
    events: list[str] = []
    config = Config()
    config.bind = [f"127.0.0.1:{free_tcp_port}"]
    config.accesslog = "-"
    config.errorlog = "-"

    async with anyio.create_task_group() as tg:
        shutdown = anyio.Event()
        await tg.start(
            lambda *, task_status: anycorn.serve(
                _make_app(events, boom=True),
                config,
                mode="wsgi",
                shutdown_trigger=shutdown.wait,
                task_status=task_status,
            )
        )
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{free_tcp_port}") as client:
            with anyio.fail_after(10), pytest.raises(httpx2.TransportError):
                await client.get("/")
        shutdown.set()

    assert events == ["opened", "closed"]
