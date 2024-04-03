from __future__ import annotations

import platform
import signal
import sys
import time
from functools import partial
from multiprocessing import get_context
from multiprocessing.connection import wait
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event as EventType
from pickle import PicklingError
from random import randint
from typing import Any, Awaitable, Callable, List, Optional

import anyio

from .config import Config, Sockets
from .lifespan import Lifespan
from .statsd import StatsdLogger
from .tcp_server import tcp_server_handler
from .typing import AppWrapper, WorkerFunc
from .udp_server import UDPServer
from .utils import (
    check_for_updates,
    check_multiprocess_shutdown_event,
    files_to_watch,
    load_application,
    raise_shutdown,
    repr_socket_addr,
    ShutdownError,
    write_pid_file,
)
from .worker_context import WorkerContext


if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


def run(config: Config) -> int:
    if config.pid_path is not None:
        write_pid_file(config.pid_path)

    worker_func: WorkerFunc
    worker_func = anyio_worker

    sockets = config.create_sockets()

    if config.use_reloader and config.workers == 0:
        raise RuntimeError("Cannot reload without workers")

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

        def shutdown(*args: Any) -> None:
            nonlocal active, shutdown_event
            shutdown_event.set()
            active = False

        processes: List[BaseProcess] = []
        while active:
            # Ignore SIGINT before creating the processes, so that they
            # inherit the signal handling. This means that the shutdown
            # function controls the shutdown.
            signal.signal(signal.SIGINT, signal.SIG_IGN)

            _populate(processes, config, worker_func, sockets, shutdown_event, ctx)

            for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
                if hasattr(signal, signal_name):
                    signal.signal(getattr(signal, signal_name), shutdown)

            if config.use_reloader:
                files = files_to_watch()
                while True:
                    finished = wait((process.sentinel for process in processes), timeout=1)
                    updated = check_for_updates(files)
                    if updated:
                        shutdown_event.set()
                        for process in processes:
                            process.join()
                        shutdown_event.clear()
                        break
                    if len(finished) > 0:
                        break
            else:
                wait(process.sentinel for process in processes)

            exitcode = _join_exited(processes)
            if exitcode != 0:
                shutdown_event.set()
                active = False

        for process in processes:
            process.terminate()

        exitcode = _join_exited(processes) if exitcode != 0 else exitcode

        for sock in sockets.secure_sockets:
            sock.close()

        for sock in sockets.insecure_sockets:
            sock.close()

    return exitcode


def _populate(
    processes: List[BaseProcess],
    config: Config,
    worker_func: WorkerFunc,
    sockets: Sockets,
    shutdown_event: EventType,
    ctx: BaseContext,
) -> None:
    for _ in range(config.workers - len(processes)):
        process = ctx.Process(  # type: ignore
            target=worker_func,
            kwargs={"config": config, "shutdown_event": shutdown_event, "sockets": sockets},
        )
        process.daemon = True
        try:
            process.start()
        except PicklingError as error:
            raise RuntimeError(
                "Cannot pickle the config, see https://docs.python.org/3/library/pickle.html#pickle-picklable"  # noqa: E501
            ) from error
        processes.append(process)
        if platform.system() == "Windows":
            time.sleep(0.1)


def _join_exited(processes: List[BaseProcess]) -> int:
    exitcode = 0
    for index in reversed(range(len(processes))):
        worker = processes[index]
        if worker.exitcode is not None:
            worker.join()
            exitcode = worker.exitcode if exitcode == 0 else exitcode
            del processes[index]

    return exitcode


async def worker_serve(
    app: AppWrapper,
    config: Config,
    *,
    sockets: Optional[Sockets] = None,
    shutdown_trigger: Optional[Callable[..., Awaitable[None]]] = None,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    config.set_statsd_logger_class(StatsdLogger)

    lifespan = Lifespan(app, config)
    max_requests = None
    if config.max_requests is not None:
        max_requests = config.max_requests + randint(0, config.max_requests_jitter)
    context = WorkerContext(max_requests)

    async with anyio.create_task_group() as lifespan_nursery:
        await lifespan_nursery.start(lifespan.handle_lifespan)
        await lifespan.wait_for_startup()

        async with anyio.create_task_group() as server_nursery:
            if sockets is None:
                sockets = config.create_sockets()
                for sock in sockets.secure_sockets:
                    sock.listen(config.backlog)
                for sock in sockets.insecure_sockets:
                    sock.listen(config.backlog)

            ssl_context = config.create_ssl_context()
            listeners = []
            binds = []
            for sock in sockets.secure_sockets:
                listeners.append(
                    trio.SSLListener(
                        trio.SocketListener(trio.socket.from_stdlib_socket(sock)),
                        ssl_context,
                        https_compatible=True,
                    )
                )
                bind = repr_socket_addr(sock.family, sock.getsockname())
                binds.append(f"https://{bind}")
                await config.log.info(f"Running on https://{bind} (CTRL + C to quit)")

            for sock in sockets.insecure_sockets:
                asynclib = anyio._core._eventloop.get_async_backend()
                listener = asynclib.create_tcp_listener(sock)
                listeners.append(listener)
                bind = repr_socket_addr(sock.family, sock.getsockname())
                binds.append(f"http://{bind}")
                await config.log.info(f"Running on http://{bind} (CTRL + C to quit)")

            for sock in sockets.quic_sockets:
                await server_nursery.start(UDPServer(app, config, context, sock).run)
                bind = repr_socket_addr(sock.family, sock.getsockname())
                await config.log.info(f"Running on https://{bind} (QUIC) (CTRL + C to quit)")

            task_status.started(binds)
            try:
                async with anyio.create_task_group() as nursery:
                    if shutdown_trigger is not None:
                        nursery.start_soon(raise_shutdown, shutdown_trigger)
                    nursery.start_soon(raise_shutdown, context.terminate.wait)

                    for listener in listeners:
                        nursery.start_soon(
                            partial(
                                listener.serve,
                                tcp_server_handler(app, config, context),
                            ),
                        )

                    await anyio.Event().wait()
            except BaseExceptionGroup as error:
                _, other_errors = error.split((ShutdownError, KeyboardInterrupt))
                if other_errors is not None:
                    raise other_errors
            finally:
                await context.terminated.set()
                server_nursery.cancel_scope.deadline = anyio.current_time() + config.graceful_timeout

        await lifespan.wait_for_shutdown()
        lifespan_nursery.cancel_scope.cancel()


def anyio_worker(
    config: Config, sockets: Optional[Sockets] = None, shutdown_event: Optional[EventType] = None
) -> None:
    if sockets is not None:
        for sock in sockets.secure_sockets:
            sock.listen(config.backlog)
        for sock in sockets.insecure_sockets:
            sock.listen(config.backlog)
    app = load_application(config.application_path, config.wsgi_max_body_size)

    shutdown_trigger = None
    if shutdown_event is not None:
        shutdown_trigger = partial(check_multiprocess_shutdown_event, shutdown_event, anyio.sleep)

    anyio.run(partial(worker_serve, app, config, sockets=sockets, shutdown_trigger=shutdown_trigger))
