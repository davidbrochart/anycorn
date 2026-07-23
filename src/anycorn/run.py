"""Entry points for running Anycorn workers."""

from __future__ import annotations

import asyncio
import platform
import signal
import sys
import time
from contextlib import AsyncExitStack, ExitStack
from functools import partial
from multiprocessing import get_context
from multiprocessing.connection import wait
from pickle import PicklingError
from random import randint
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import anyio.streams.tls
import sniffio

from .datagram import wrap_datagram_socket
from .lifespan import Lifespan
from .statsd import StatsdLogger
from .tcp_server import tcp_server_handler
from .typing import AppWrapper, ConnectionState, LifespanState, WorkerFunc
from .udp_server import UDPServer
from .utils import (
    ShutdownError,
    check_for_updates,
    check_multiprocess_shutdown_event,
    files_to_watch,
    load_application,
    raise_shutdown,
    repr_socket_addr,
    write_pid_file,
)
from .worker_context import WorkerContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from multiprocessing.context import BaseContext
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event as EventType

    from .config import Config, Sockets

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


def run(config: Config) -> int:  # noqa: C901, PLR0912, PLR0915
    """Start the server, blocking until it exits, and return an exit code."""
    if config.pid_path is not None:
        write_pid_file(config.pid_path)

    worker_func: WorkerFunc
    worker_func = anyio_worker

    sockets = config.create_sockets()

    with ExitStack() as cleanup_stack:
        # Whatever happens next - a worker failing to spawn, the reloader raising -
        # the parent is left holding these, and each is closed independently so one
        # refusing does not strand the rest
        for sock in (*sockets.secure_sockets, *sockets.insecure_sockets, *sockets.quic_sockets):
            cleanup_stack.enter_context(sock)

        if config.use_reloader and config.workers == 0:
            msg = "Cannot reload without workers"
            raise RuntimeError(msg)

        exitcode = 0
        if config.workers == 0:
            worker_func(config, sockets)
        else:
            if config.use_reloader:
                # Load the application so that the correct paths are checked for
                # changes, but only when the reloader is being used.
                load_application(config.application_path, config.wsgi_max_body_size)

            ctx = get_context("spawn")

            active = True
            shutdown_event = ctx.Event()

            def shutdown(*_args: Any) -> None:  # noqa: ANN401
                nonlocal active
                shutdown_event.set()
                active = False

            def reload(*_args: Any) -> None:  # noqa: ANN401
                shutdown_event.set()
                for process in processes:
                    process.join()
                shutdown_event.clear()

            processes: list[BaseProcess] = []
            # Registered after the sockets, so it unwinds first: signal the children,
            # then close what the parent is still holding. A worker that fails to
            # spawn would otherwise leave its siblings running
            cleanup_stack.callback(_terminate, processes)
            while active:
                # Ignore SIGINT before creating the processes, so that they
                # inherit the signal handling. This means that the shutdown
                # function controls the shutdown.
                signal.signal(signal.SIGINT, signal.SIG_IGN)

                _populate(processes, config, worker_func, sockets, shutdown_event, ctx)

                for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                    if hasattr(signal, signal_name):
                        signal.signal(getattr(signal, signal_name), shutdown)

                if hasattr(signal, "SIGHUP"):
                    signal.signal(signal.SIGHUP, reload)

                if config.use_reloader:
                    files = files_to_watch()
                    while True:
                        finished = wait((process.sentinel for process in processes), timeout=1)
                        updated = check_for_updates(files)
                        if updated:
                            reload()
                            break
                        if len(finished) > 0:
                            break
                else:
                    wait(process.sentinel for process in processes)

                exitcode = _join_exited(processes)
                if exitcode != 0:
                    shutdown_event.set()
                    active = False

            # Only reap again when nothing has failed yet: a non-zero exitcode has
            # already been captured from a worker the loop reaped and removed, so
            # re-joining the now-empty list would return 0 and mask the failure - a
            # reload onto a SyntaxError then exited 0 instead of erroring (#269).
            exitcode = _join_exited(processes) if exitcode == 0 else exitcode
        return exitcode


def _populate(  # noqa: PLR0913
    processes: list[BaseProcess],
    config: Config,
    worker_func: WorkerFunc,
    sockets: Sockets,
    shutdown_event: EventType,
    ctx: BaseContext,
) -> None:
    for _ in range(config.workers - len(processes)):
        process = ctx.Process(  # type: ignore[attr-defined]
            target=worker_func,
            kwargs={"config": config, "shutdown_event": shutdown_event, "sockets": sockets},
        )
        process.daemon = config.daemon
        try:
            process.start()
        except PicklingError as error:
            msg = "Cannot pickle the config, see https://docs.python.org/3/library/pickle.html#pickle-picklable"
            raise RuntimeError(msg) from error
        processes.append(process)
        if platform.system() == "Windows":
            time.sleep(0.1)


def _terminate(processes: list[BaseProcess]) -> None:
    """Signal every worker still running. Reaped ones have already left the list."""
    for process in processes:
        process.terminate()


def _join_exited(processes: list[BaseProcess]) -> int:
    exitcode = 0
    for index in reversed(range(len(processes))):
        worker = processes[index]
        if worker.exitcode is not None:
            worker.join()
            exitcode = worker.exitcode if exitcode == 0 else exitcode
            del processes[index]

    return exitcode


async def _wait_for_shutdown_signal(signals: tuple[signal.Signals, ...]) -> None:
    """Return once the first of *signals* is received.

    open_signal_receiver drives asyncio's loop.add_signal_handler, which raises
    NotImplementedError on Windows. There we fall back to signal.signal, which
    does work on Windows for these console signals - the same fallback
    hypercorn's own asyncio backend makes, so --workers 0 stays gracefully
    shutdownable there instead of crashing the worker on startup.
    """
    try:
        with anyio.open_signal_receiver(*signals) as received_signals:
            async for _signum in received_signals:
                return
    except NotImplementedError:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with ExitStack() as stack:
            for sig in signals:
                stack.callback(signal.signal, sig, signal.getsignal(sig))
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(event.set))
            await event.wait()


async def worker_serve(  # noqa: C901, PLR0912, PLR0915
    app: AppWrapper,
    config: Config,
    *,
    sockets: Sockets | None = None,
    shutdown_trigger: Callable[..., Awaitable[None]] | None = None,
    task_status: anyio.abc.TaskStatus[list[str]] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Run the server workers, handling lifespan and connections."""
    config.set_statsd_logger_class(StatsdLogger)

    if shutdown_trigger is None and sniffio.current_async_library() == "asyncio":
        # Matches hypercorn's asyncio-backend fallback: without this, Ctrl-C/SIGTERM
        # would bypass the graceful, graceful_timeout-respecting shutdown path every
        # other trigger source here uses, and SIGTERM would go entirely unhandled.
        # Not ported for trio: hypercorn's own trio backend has no equivalent,
        # relying solely on trio's built-in SIGINT-to-cancellation behaviour.
        shutdown_trigger = partial(
            _wait_for_shutdown_signal,
            tuple(
                sig
                for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None))
                if sig is not None
            ),
        )

    lifespan_state: LifespanState = {}
    lifespan = Lifespan(app, config, lifespan_state)
    max_requests = None
    if config.max_requests is not None:
        max_requests = config.max_requests + randint(0, config.max_requests_jitter)  # noqa: S311
    context = WorkerContext(max_requests)

    async with config.log, anyio.create_task_group() as lifespan_tg:
        await lifespan_tg.start(lifespan.handle_lifespan)
        await lifespan.wait_for_startup()

        async with anyio.create_task_group() as server_tg, AsyncExitStack() as socket_stack:
            if sockets is None:
                sockets = config.create_sockets()
                for sock in sockets.secure_sockets:
                    sock.listen(config.backlog)
                for sock in sockets.insecure_sockets:
                    sock.listen(config.backlog)

            ssl_context = config.create_ssl_context()
            listeners: list[anyio.abc.SocketListener | anyio.streams.tls.TLSListener] = []
            binds = []
            for secure_sock in sockets.secure_sockets:
                assert ssl_context is not None
                asynclib = anyio._core._eventloop.get_async_backend()  # noqa: SLF001  # ty:ignore[possibly-missing-attribute]
                secure_listener = anyio.streams.tls.TLSListener(
                    asynclib.create_tcp_listener(secure_sock),
                    ssl_context,
                    True,  # noqa: FBT003
                    config.ssl_handshake_timeout,
                )
                listeners.append(await socket_stack.enter_async_context(secure_listener))
                bind = repr_socket_addr(secure_sock.family, secure_sock.getsockname())
                url = f"https://{bind}"
                binds.append(url)
                await config.log.info("Running on %s (CTRL + C to quit)", url)

            for insecure_sock in sockets.insecure_sockets:
                asynclib = anyio._core._eventloop.get_async_backend()  # noqa: SLF001  # ty:ignore[possibly-missing-attribute]
                insecure_listener = asynclib.create_tcp_listener(insecure_sock)
                listeners.append(await socket_stack.enter_async_context(insecure_listener))
                bind = repr_socket_addr(insecure_sock.family, insecure_sock.getsockname())
                url = f"http://{bind}"
                binds.append(url)
                await config.log.info("Running on %s (CTRL + C to quit)", url)

            udp_servers = []
            for quic_sock in sockets.quic_sockets:
                udp_socket = await wrap_datagram_socket(quic_sock)
                socket_stack.push_async_callback(udp_socket.aclose)
                udp_servers.append(
                    UDPServer(app, config, context, lifespan_state.copy(), udp_socket)
                )
                bind = repr_socket_addr(quic_sock.family, quic_sock.getsockname())
                url = f"https://{bind}"
                binds.append(url)
                await config.log.info("Running on %s (QUIC, CTRL + C to quit)", url)

            task_status.started(binds)
            try:
                async with anyio.create_task_group() as tg:
                    if shutdown_trigger is not None:
                        tg.start_soon(raise_shutdown, shutdown_trigger)
                    tg.start_soon(raise_shutdown, context.terminate.wait)

                    for udp_server in udp_servers:
                        await tg.start(udp_server.run)

                    for listener in listeners:
                        tg.start_soon(
                            partial(
                                listener.serve,
                                tcp_server_handler(
                                    app, config, context, ConnectionState(lifespan_state.copy())
                                ),
                            ),
                        )

                    await anyio.Event().wait()
            except BaseExceptionGroup as error:
                _, other_errors = error.split((ShutdownError, KeyboardInterrupt))
                if other_errors is not None:
                    raise other_errors from error
            finally:
                await context.terminated.set()
                server_tg.cancel_scope.deadline = anyio.current_time() + config.graceful_timeout

        await lifespan.wait_for_shutdown()
        lifespan_tg.cancel_scope.cancel()


def anyio_worker(
    config: Config, sockets: Sockets | None = None, shutdown_event: EventType | None = None
) -> None:
    """Run the anyio worker, loading the application and serving requests."""
    if sockets is not None:
        for sock in sockets.secure_sockets:
            sock.listen(config.backlog)
        for sock in sockets.insecure_sockets:
            sock.listen(config.backlog)
    app = load_application(config.application_path, config.wsgi_max_body_size)

    shutdown_trigger = None
    if shutdown_event is not None:
        shutdown_trigger = partial(check_multiprocess_shutdown_event, shutdown_event, anyio.sleep)

    anyio.run(
        partial(worker_serve, app, config, sockets=sockets, shutdown_trigger=shutdown_trigger),
        backend=config.worker_class,
    )
