"""Tests for TCPServer connection teardown."""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING, Any

import anyio
import h11
import pytest

from anycorn.app_wrappers import ASGIWrapper
from anycorn.config import Config
from anycorn.tcp_server import TCPServer
from anycorn.worker_context import WorkerContext

from .helpers import SANITY_BODY, MemorySocketStream, memory_socket_stream_pair, sanity_framework

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


class _UnreachableOnCloseStream(MemorySocketStream):
    """A stream whose close raises a network-unreachable OSError.

    asyncio maps only a handful of errno (ECONNRESET, EPIPE, ...) to ConnectionError;
    EHOSTUNREACH and its kin (ENETUNREACH, ETIMEDOUT) stay plain OSError, as an abrupt
    client disconnect - a deleted pod, a pulled cable - can produce.
    """

    def __init__(
        self,
        receive_stream: MemoryObjectReceiveStream[bytes],
        send_stream: MemoryObjectSendStream[bytes],
        attributes: Mapping[Any, Callable[[], Any]],
    ) -> None:
        super().__init__(receive_stream, send_stream, attributes)
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True
        await super().aclose()
        raise OSError(errno.EHOSTUNREACH, "No route to host")


@pytest.mark.anyio
async def test_a_connection_whose_close_fails_does_not_crash_the_server() -> None:
    """The teardown OSError must not escape run(), which is where it would land.

    _close() runs from run()'s finally - outside its own `except OSError` - and from
    protocol_send, so an escaping OSError crashes the connection task or propagates
    back into the ASGI app (https://github.com/pgjones/hypercorn/issues/361). Driving
    a whole request through means run() really is what has to survive it, rather than
    _close() being called directly and the caller assumed.
    """
    client_stream, plain_server_stream = memory_socket_stream_pair()
    server_stream = _UnreachableOnCloseStream(
        plain_server_stream._receive_stream,
        plain_server_stream._send_stream,
        plain_server_stream.extra_attributes,
    )
    server = TCPServer(
        ASGIWrapper(sanity_framework), Config(), WorkerContext(None), {}, server_stream
    )

    client = h11.Connection(h11.CLIENT)
    statuses = []
    # Only the client end is closed here: closing the server end is the server's job,
    # and this one raises by design, so doing it again would be the test failing itself
    async with client_stream:
        # An escaping OSError propagates out of the task group, which is the assertion
        with anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(server.run)

                await client_stream.send_all(
                    client.send(
                        h11.Request(
                            method="POST",
                            target="/",
                            headers=[
                                (b"host", b"anycorn"),
                                (b"connection", b"close"),
                                (b"content-length", b"%d" % len(SANITY_BODY)),
                            ],
                        )
                    )
                )
                await client_stream.send_all(client.send(h11.Data(data=SANITY_BODY)))
                await client_stream.send_all(client.send(h11.EndOfMessage()))

                while True:
                    event = client.next_event()
                    if event is h11.NEED_DATA:
                        client.receive_data(await client_stream.receive_some(4096))
                    elif isinstance(event, h11.Response):
                        statuses.append(event.status_code)
                    elif isinstance(event, h11.ConnectionClosed):
                        break

                # _read_data stays parked on the memory stream once it is closed, as
                # serve_in_memory also has to allow for, so stop the server rather
                # than wait on a run() that will not return
                task_group.cancel_scope.cancel()

    # The request was served, and the failing teardown did not take run() down with it
    assert statuses == [200]
    assert server_stream.aclose_called
