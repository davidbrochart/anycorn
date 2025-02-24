from __future__ import annotations

import pytest
from _pytest.monkeypatch import MonkeyPatch

import anycorn.config
from anycorn.typing import ConnectionState, HTTPScope


@pytest.fixture(autouse=True)
def _time(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(anycorn.config, "time", lambda: 5000)


@pytest.fixture(name="http_scope")
def _http_scope() -> HTTPScope:
    return {
        "type": "http",
        "asgi": {},
        "http_version": "2",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"a=b",
        "root_path": "",
        "headers": [
            (b"User-Agent", b"Anycorn"),
            (b"X-Anycorn", b"Anycorn"),
            (b"Referer", b"anycorn"),
        ],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": ConnectionState({}),
    }
