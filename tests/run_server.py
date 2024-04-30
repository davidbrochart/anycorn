import sys
from importlib import import_module
from typing import Awaitable, Callable, Optional

host, port, backend, server = sys.argv[1:]


async def app(
    scope: dict, receive: Callable[[], Awaitable], send: Callable[[dict], Awaitable]
) -> None:
    while True:
        event = await receive()
        event_type = event["type"]
        if event_type == "http.request" and not event.get("more_body", False):
            await send_data(send)
            break
        elif event_type == "http.disconnect":
            break
        elif event_type == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event_type == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            break


async def send_data(send: Callable[[dict], Awaitable]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"5")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"Hello",
            "more_body": False,
        }
    )


if server == "anycorn":
    import anyio
    from anycorn import serve as anycorn_serve
    from anycorn.config import Config as AnycornConfig
    from anyio import run as anycorn_run
    from anyio.abc import AsyncBackend

    modulename = "anyio._backends._" + backend
    module = import_module(modulename)
    async_backend = getattr(module, "backend_class")

    def get_async_backend(asynclib_name: Optional[str] = None) -> AsyncBackend:
        return async_backend

    anyio._core._eventloop.get_async_backend = get_async_backend

    anycorn_config = AnycornConfig()
    anycorn_config.bind = [f"{host}:{port}"]

    anycorn_run(anycorn_serve, app, anycorn_config)  # type: ignore[arg-type]
else:
    from hypercorn import Config as HypercornConfig

    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"{host}:{port}"]

    if backend == "trio":
        from hypercorn.trio import serve as hypercorn_trio_serve
        from trio import run as hypercorn_trio_run

        hypercorn_trio_run(hypercorn_trio_serve, app, hypercorn_config)
    else:
        from asyncio import run as hypercorn_asyncio_run

        from hypercorn.asyncio import serve as hypercorn_asyncio_serve

        hypercorn_asyncio_run(hypercorn_asyncio_serve(app, hypercorn_config))  # type: ignore[arg-type]
