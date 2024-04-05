# Anycorn

Anycorn is an [ASGI](https://asgi.readthedocs.io) and WSGI web server based on the sans-io hyper,
[h11](https://github.com/python-hyper/h11), [h2](https://github.com/python-hyper/hyper-h2) and
[wsproto](https://github.com/python-hyper/wsproto) libraries and inspired by Gunicorn. Anycorn
supports HTTP/1, HTTP/2, WebSockets (over HTTP/1 and HTTP/2), ASGI, and WSGI specifications.
Anycorn utilises [AnyIO](https://anyio.readthedocs.io) worker types.

Anycorn can optionally serve the current draft of the HTTP/3 specification using the
[aioquic](https://github.com/aiortc/aioquic) library. To enable this, install the `h3` optional
extra with `pip install anycorn[h3]` and then choose a quic binding, e.g.
`anycorn --quic-bind localhost:4433 ...`.

Anycorn is a fork of [Hypercorn](https://github.com/pgjones/hypercorn) where asyncio and Trio
compatibility is delegated to AnyIO, instead of having a separate code base for each. Anycorn forked
from version 0.16.0 of Hypercorn.

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

See the Hypercorn's
[documentation](https://hypercorn.readthedocs.io/en/latest/how_to_guides/api_usage.html) for more
details.

## Testing

The best way to test Anycorn is with [Tox](https://tox.readthedocs.io):

```bash
pipenv install tox
tox
```

This will check the code style and run the tests.
