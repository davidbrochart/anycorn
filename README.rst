Anycorn
=======

.. image:: https://github.com/davidbrochart/anycorn/raw/main/artwork/logo.png
   :alt: Hypercorn logo

|Build Status| |docs| |pypi| |http| |python| |license|

Anycorn is an `ASGI
<https://github.com/django/asgiref/blob/main/specs/asgi.rst>`_ and
WSGI web server based on the sans-io hyper, `h11
<https://github.com/python-hyper/h11>`_, `h2
<https://github.com/python-hyper/hyper-h2>`_, and `wsproto
<https://github.com/python-hyper/wsproto>`_ libraries and inspired by
Gunicorn. Anycorn supports HTTP/1, HTTP/2, WebSockets (over HTTP/1
and HTTP/2), ASGI, and WSGI specifications. Anycorn utilises
anyio worker types.

Anycorn can optionally serve the current draft of the HTTP/3
specification using the `aioquic
<https://github.com/aiortc/aioquic/>`_ library. To enable this install
the ``h3`` optional extra, ``pip install anycorn[h3]`` and then
choose a quic binding e.g. ``anycorn --quic-bind localhost:4433
...``.

Anycorn is a fork of `Hypercorn
<https://github.com/pgjones/hypercorn>`_ that replaces asyncio where
asyncio and Trio implementations are replaced with AnyIO.
Anycorn forked from version 0.16.0 of Hypercorn.

Quickstart
----------

Anycorn can be installed via `pip
<https://docs.python.org/3/installing/index.html>`_,

.. code-block:: console

    $ pip install anycorn

and requires Python 3.8 or higher.

With anycorn installed ASGI frameworks (or apps) can be served via
Anycorn via the command line,

.. code-block:: console

    $ anycorn module:app

Alternatively Anycorn can be used programatically,

.. code-block:: python

    import anyio
    from anycorn.config import Config
    from anycorn import serve

    from module import app

    anyio.run(serve, app, Config())

learn more in the `API usage
<https://hypercorn.readthedocs.io/en/latest/how_to_guides/api_usage.html>`_
docs.

Contributing
------------

Anycorn is developed on `Github
<https://github.com/davidbrochart/anycorn>`_. If you come across an issue,
or have a feature request please open an `issue
<https://github.com/davidbrochart/anycorn/issues>`_.  If you want to
contribute a fix or the feature-implementation please do (typo fixes
welcome), by proposing a `pull request
<https://github.com/davidbrochart/anycorn/merge_requests>`_.

Testing
~~~~~~~

The best way to test Anycorn is with `Tox
<https://tox.readthedocs.io>`_,

.. code-block:: console

    $ pipenv install tox
    $ tox

this will check the code style and run the tests.

Help
----

The Anycorn `documentation <https://hypercorn.readthedocs.io>`_ is
the best place to start, after that try searching stack overflow, if
you still can't find an answer please `open an issue
<https://github.com/davidbrochart/anycorn/issues>`_.


.. |Build Status| image:: https://github.com/davidbrochart/anycorn/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/davidbrochart/anycorn/commits/main

.. |docs| image:: https://img.shields.io/badge/docs-passing-brightgreen.svg
   :target: https://hypercorn.readthedocs.io

.. |pypi| image:: https://img.shields.io/pypi/v/hypercorn.svg
   :target: https://pypi.python.org/pypi/anycorn/

.. |http| image:: https://img.shields.io/badge/http-1.0,1.1,2-orange.svg
   :target: https://en.wikipedia.org/wiki/Hypertext_Transfer_Protocol

.. |python| image:: https://img.shields.io/pypi/pyversions/hypercorn.svg
   :target: https://pypi.python.org/pypi/anycorn/

.. |license| image:: https://img.shields.io/badge/license-MIT-blue.svg
   :target: https://github.com/davidbrochart/anycorn/blob/main/LICENSE
