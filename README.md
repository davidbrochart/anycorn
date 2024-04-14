# Anycorn

Anycorn is a fork of [Hypercorn](https://github.com/pgjones/hypercorn) where `asyncio` and
[Trio](https://trio.readthedocs.io) compatibility is delegated to AnyIO, instead of having a
separate code base for each. Anycorn forked from version 0.16.0 of Hypercorn.

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
