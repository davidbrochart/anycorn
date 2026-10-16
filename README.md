# Anycorn

Anycorn is a fork of [Hypercorn](https://github.com/pgjones/hypercorn) where `asyncio` and
[Trio](https://trio.readthedocs.io) compatibility is delegated to AnyIO, instead of having a
separate code base for each. Anycorn forked from version 0.16.0 of Hypercorn. This fork supports [tls extension](https://asgi.readthedocs.io/en/latest/specs/tls.html).

## Quickstart

Anycorn can be installed via [pip](https://docs.python.org/3/installing/index.html):

```bash
pip install anycorn
```

and requires Python 3.8 or higher.

With Anycorn, installed ASGI frameworks (or apps) can be served via the command line:

```bash
anycorn module:app
```

Alternatively, Anycorn can be used programatically:

```py
import anyio
from anycorn.config import Config
from anycorn import serve

from module import app

anyio.run(serve, app, Config())
```

See Hypercorn's
[documentation](https://hypercorn.readthedocs.io/en/latest/how_to_guides/api_usage.html) for more
details.

## Worker recycling

Workers can be recycled after a fixed number of requests with `--max-requests`, or after their
current resident set size exceeds a limit with `--max-rss`:

```bash
anycorn --workers 4 --max-rss 512 module:app
```

`max_rss` is specified in MiB and is disabled by default. Anycorn samples the worker RSS
periodically and uses the same graceful shutdown path as request-count recycling, so in-flight
requests can drain until `graceful_timeout` before the master process respawns the worker. When
`--workers 0` is used, no master process exists to respawn the worker; exceeding `max_rss` stops
that single worker, matching `max_requests` behavior.
