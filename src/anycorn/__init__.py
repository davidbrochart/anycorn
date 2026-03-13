from __future__ import annotations

import importlib.metadata
import warnings
from typing import TYPE_CHECKING, Literal

import anyio

from .config import Config
from .run import worker_serve
from .utils import wrap_app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .typing import Framework

__all__ = ("Config", "serve")
__version__ = importlib.metadata.version("anycorn")


async def serve(
    app: Framework,
    config: Config,
    *,
    shutdown_trigger: Callable[..., Awaitable[None]] | None = None,
    task_status: anyio.abc.TaskStatus[list[str]] = anyio.TASK_STATUS_IGNORED,
    mode: Literal["asgi", "wsgi"] | None = None,
) -> None:
    """Serve an ASGI framework app given the config.

    This allows for a programmatic way to serve an ASGI framework, it
    can be used via,

    .. code-block:: python

        anyio.run(serve, app, config)

    It is assumed that the event-loop is configured before calling
    this function, therefore configuration values that relate to loop
    setup or process setup are ignored.

    Arguments:
        app: The ASGI application to serve.
        config: A Hypercorn configuration object.
        shutdown_trigger: This should return to trigger a graceful
            shutdown.
        task_status: The task status object, used when this is called
            as a task via ``nursery.start(serve, app, config)``.
        mode: Specify if the app is WSGI or ASGI.

    """
    if config.debug:
        warnings.warn("The config `debug` has no affect when using serve", Warning, stacklevel=2)
    if config.workers != 1:
        warnings.warn("The config `workers` has no affect when using serve", Warning, stacklevel=2)

    await worker_serve(
        wrap_app(app, config.wsgi_max_body_size, mode),
        config,
        shutdown_trigger=shutdown_trigger,
        task_status=task_status,
    )
