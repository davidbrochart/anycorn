"""Tests for the statsd logger."""

from __future__ import annotations

import math
import socket
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import anyio.lowlevel
import pytest
from anyio.abc import SocketAttribute

from anycorn.config import Config
from anycorn.statsd import StatsdLogger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _StatsdDaemon:
    """A real UDP listener standing in for a statsd daemon, collecting the datagrams sent."""

    def __init__(self, sock: anyio.abc.UDPSocket) -> None:
        self._sock = sock
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)

    @property
    def statsd_host(self) -> str:
        host, port = self._sock.extra(SocketAttribute.local_address)  # noqa: S610
        return f"{host}:{port}"

    async def serve(self) -> None:
        """Feed datagrams into the stream as they arrive, until the task group is cancelled."""
        while True:
            data, _ = await self._sock.receive()
            await self._send.send(data)

    async def wait_for(self, count: int) -> list[bytes]:
        """Wait until *count* datagrams have arrived, then return them."""
        with anyio.fail_after(5):
            return [await self._receive.receive() for _ in range(count)]


@pytest.fixture
async def statsd_daemon() -> AsyncIterator[_StatsdDaemon]:
    """Run a UDP statsd daemon on loopback for the duration of a test."""
    async with await anyio.create_udp_socket(local_host="127.0.0.1") as sock:
        daemon = _StatsdDaemon(sock)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(daemon.serve)
            yield daemon
            task_group.cancel_scope.cancel()


class _CapturingStatsd(StatsdLogger):
    """A StatsdLogger that records the datagrams it would send instead of opening a socket."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.sent: list[bytes] = []

    async def _socket_send(self, message: bytes) -> None:
        self.sent.append(message)


def _capturing(**overrides: Any) -> _CapturingStatsd:  # noqa: ANN401
    config = Config()
    config.statsd_host = "127.0.0.1:9125"
    for name, value in overrides.items():
        setattr(config, name, value)
    return _CapturingStatsd(config)


def test_a_non_empty_prefix_gains_a_trailing_dot() -> None:
    """The prefix is joined to metric names directly, so it needs its own separator."""
    assert _capturing(statsd_prefix="myapp").prefix == "myapp."
    assert _capturing(statsd_prefix="myapp.").prefix == "myapp."
    assert _capturing().prefix == ""


@pytest.mark.anyio
async def test_each_metric_type_reaches_the_daemon_in_its_wire_format(
    statsd_daemon: _StatsdDaemon,
) -> None:
    """Every metric helper's datagram must arrive at a real listener in statsd's format."""
    config = Config()
    config.statsd_host = statsd_daemon.statsd_host
    config.statsd_prefix = "app"
    logger = StatsdLogger(config)
    try:
        await logger.gauge("g", 3)
        await logger.increment("c", 2)
        await logger.decrement("c", 2)
        await logger.histogram("h", 12.5)
        received = await statsd_daemon.wait_for(4)
    finally:
        await logger.aclose()

    # UDP does not promise ordering, so compare as a set of distinct datagrams.
    assert set(received) == {
        b"app.g:3|g",
        b"app.c:2|c|@1.0",
        b"app.c:-2|c|@1.0",
        b"app.h:12.5|ms",
    }


@pytest.mark.anyio
async def test_dogstatsd_tags_reach_the_daemon(statsd_daemon: _StatsdDaemon) -> None:
    config = Config()
    config.statsd_host = statsd_daemon.statsd_host
    config.dogstatsd_tags = "env:prod,role:web"
    logger = StatsdLogger(config)
    try:
        await logger.gauge("g", 1)
        received = await statsd_daemon.wait_for(1)
    finally:
        await logger.aclose()

    assert received == [b"g:1|g|#env:prod,role:web"]


@pytest.mark.anyio
async def test_log_level_helpers_emit_their_counters() -> None:
    logger = _capturing()
    await logger.critical("boom")
    await logger.error("bad")
    await logger.warning("hmm")
    await logger.exception("oops")
    await logger.info("fyi")  # info/debug do not emit a metric
    await logger.debug("trace")
    assert logger.sent == [
        b"anycorn.log.critical:1|c|@1.0",
        b"anycorn.log.error:1|c|@1.0",
        b"anycorn.log.warning:1|c|@1.0",
        b"anycorn.log.exception:1|c|@1.0",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mtype", "expected"),
    [("gauge", b"m:5|g"), ("counter", b"m:5|c|@1.0"), ("histogram", b"m:5|ms")],
)
async def test_log_routes_extra_metrics_by_type(mtype: str, expected: bytes) -> None:
    logger = _capturing()
    await logger.log(20, "", extra={"metric": "m", "value": 5, "mtype": mtype})
    assert logger.sent == [expected]


@pytest.mark.anyio
async def test_log_without_metric_extra_sends_nothing() -> None:
    logger = _capturing()
    await logger.log(20, "just a message")
    await logger.log(20, "partial", extra={"metric": "m", "value": 5, "mtype": None})
    await logger.log(20, "unknown type", extra={"metric": "m", "value": 5, "mtype": "timer"})
    assert logger.sent == []


@pytest.mark.anyio
async def test_log_swallows_a_failure_to_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken metric send must not propagate out of log()."""
    logger = _capturing()

    async def boom(_name: str, _value: int) -> None:
        raise RuntimeError

    monkeypatch.setattr(logger, "gauge", boom)
    # Must not raise despite gauge blowing up.
    await logger.log(20, "msg", extra={"metric": "m", "value": 5, "mtype": "gauge"})
    assert logger.sent == []


@pytest.mark.anyio
async def test_access_emits_duration_count_and_status() -> None:
    logger = _capturing()
    request: Any = {"type": "http"}
    await logger.access(request, {"status": 200, "headers": []}, 0.25)
    assert logger.sent == [
        b"anycorn.request.duration:250.0|ms",
        b"anycorn.requests:1|c|@1.0",
        b"anycorn.request.status.200:1|c|@1.0",
    ]


@pytest.mark.anyio
async def test_access_without_a_response_still_counts_the_request() -> None:
    logger = _capturing()
    request: Any = {"type": "http"}
    await logger.access(request, None, 0.1)
    assert logger.sent == [
        b"anycorn.request.duration:100.0|ms",
        b"anycorn.requests:1|c|@1.0",
    ]


@pytest.mark.anyio
async def test_aclose_without_an_open_socket_is_a_no_op() -> None:
    """Closing a logger that never sent anything must not touch a socket that isn't there."""
    logger = _capturing()
    await logger.aclose()  # no socket was ever opened; nothing to release


def _unused_udp_port() -> int:
    """Return a port with nothing bound to it, so datagrams are refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.anyio
async def test_metrics_survive_a_daemon_that_is_not_listening() -> None:
    """A statsd daemon that is down must not break the request being measured.

    A connected UDP socket is told about ICMP port unreachable, so the send after the
    one that provoked it fails - taking down whichever request happened to be logging.
    """
    config = Config()
    config.statsd_host = f"127.0.0.1:{_unused_udp_port()}"
    logger = StatsdLogger(config)
    try:
        for _ in range(5):
            await logger.increment("anycorn.test", 1)
            await anyio.sleep(0.01)
    finally:
        await logger.aclose()


@pytest.mark.anyio
async def test_the_guarded_open_reuses_a_socket_a_peer_already_made() -> None:
    """A caller that wins into the guard, but after a peer opened, must not open again.

    Without the lock two callers emitting their first metric together would each pass the
    outer ``is None`` check and open a socket, orphaning all but one. The inner re-check
    under the lock is what stops the second open. It is driven deterministically here by a
    stand-in guard that drops a socket in on entry, so the caller reaches the inner check
    with the socket already set - the state a peer opening first would leave. A real socket
    is beside the point (and two live callers would collide on one datagram socket anyway);
    the open path onto a real listener is covered by the wire-format tests above.
    """

    class _FakeSender:
        async def send(self, message: bytes) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _PeerOpensOnEntry:
        """Stands in for the lock, opening the socket the instant the guard is entered."""

        async def __aenter__(self) -> _PeerOpensOnEntry:  # noqa: PYI034
            logger._sender = _FakeSender()  # a peer opened it first
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    config = Config()
    config.statsd_host = "127.0.0.1:9125"
    logger = StatsdLogger(config)
    logger._sender_lock = _PeerOpensOnEntry()  # type: ignore[assignment]

    await logger.increment("anycorn.test", 1)  # reaches the inner check, opens nothing
    await logger.aclose()


@pytest.mark.anyio
async def test_aclose_forcefully_releases_socket() -> None:
    """Closing in a cancelled scope must still release the socket."""
    config = Config()
    config.statsd_host = "localhost:9125"
    logger = StatsdLogger(config)
    await logger.increment("anycorn.test", 1)
    sender = logger._sender
    assert sender is not None

    await anyio.aclose_forcefully(logger)

    assert sender.socket.fileno() == -1
