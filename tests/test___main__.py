from __future__ import annotations

import inspect
import os
from unittest.mock import Mock

import anycorn.__main__
import pytest
from _pytest.monkeypatch import MonkeyPatch
from anycorn.config import Config


def test_load_config_none() -> None:
    assert isinstance(anycorn.__main__._load_config(None), Config)


def test_load_config_pyfile(monkeypatch: MonkeyPatch) -> None:
    mock_config = Mock()
    monkeypatch.setattr(anycorn.__main__, "Config", mock_config)
    anycorn.__main__._load_config("file:assets/config.py")
    mock_config.from_pyfile.assert_called()


def test_load_config_pymodule(monkeypatch: MonkeyPatch) -> None:
    mock_config = Mock()
    monkeypatch.setattr(anycorn.__main__, "Config", mock_config)
    anycorn.__main__._load_config("python:assets.config")
    mock_config.from_object.assert_called()


def test_load_config(monkeypatch: MonkeyPatch) -> None:
    mock_config = Mock()
    monkeypatch.setattr(anycorn.__main__, "Config", mock_config)
    anycorn.__main__._load_config("assets/config")
    mock_config.from_toml.assert_called()


@pytest.mark.parametrize(
    "flag, set_value, config_key",
    [
        ("--access-logformat", "jeff", "access_log_format"),
        ("--backlog", 5, "backlog"),
        ("--ca-certs", "/path", "ca_certs"),
        ("--certfile", "/path", "certfile"),
        ("--ciphers", "DHE-RSA-AES128-SHA", "ciphers"),
        ("--keep-alive", 20, "keep_alive_timeout"),
        ("--keyfile", "/path", "keyfile"),
        ("--pid", "/path", "pid_path"),
        ("--root-path", "/path", "root_path"),
        ("--workers", 2, "workers"),
    ],
)
def test_main_cli_override(
    flag: str, set_value: str, config_key: str, monkeypatch: MonkeyPatch
) -> None:
    run_multiple = Mock()
    monkeypatch.setattr(anycorn.__main__, "run", run_multiple)
    path = os.path.join(os.path.dirname(__file__), "assets/config_ssl.py")
    raw_config = Config.from_pyfile(path)

    anycorn.__main__.main(["--config", f"file:{path}", flag, str(set_value), "asgi:App"])
    run_multiple.assert_called()
    config = run_multiple.call_args_list[0][0][0]

    for name, value in inspect.getmembers(raw_config):
        if (
            not inspect.ismethod(value)
            and not name.startswith("_")
            and name not in {"log", config_key}
        ):
            assert getattr(raw_config, name) == getattr(config, name)
    assert getattr(config, config_key) == set_value


def test_verify_mode_conversion(monkeypatch: MonkeyPatch) -> None:
    run_multiple = Mock()
    monkeypatch.setattr(anycorn.__main__, "run", run_multiple)

    with pytest.raises(SystemExit):
        anycorn.__main__.main(["--verify-mode", "CERT_UNKNOWN", "asgi:App"])

    anycorn.__main__.main(["--verify-mode", "CERT_REQUIRED", "asgi:App"])
    run_multiple.assert_called()
