"""Anycorn must import and serve without the optional h3 dependency installed."""

from __future__ import annotations

import subprocess
import sys

# Run in a subprocess so aioquic can be hidden before anycorn is imported at all
_SCRIPT = """
import sys


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "aioquic" or name.startswith("aioquic."):
            msg = f"hidden for this test: {name}"
            raise ImportError(msg)
        return None


sys.meta_path.insert(0, _Blocker())
for name in [n for n in sys.modules if n.startswith("aioquic")]:
    del sys.modules[name]

import anyio

import anycorn
from anycorn.config import Config


async def app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def main():
    config = Config()
    config.bind = ["127.0.0.1:0"]
    shutdown = anyio.Event()
    async with anyio.create_task_group() as tg:
        binds = await tg.start(
            lambda *, task_status: anycorn.serve(
                app, config, shutdown_trigger=shutdown.wait, task_status=task_status
            )
        )
        assert binds, "expected a bind"
        shutdown.set()

    print("SERVED")


anyio.run(main)
"""


def test_serves_without_aioquic() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SERVED" in result.stdout
    assert "aioquic" not in result.stderr
