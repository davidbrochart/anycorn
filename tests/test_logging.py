"""Tests for anycorn logging functionality and access log atom formatting."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from anycorn.config import Config
from anycorn.logging import AccessLogAtoms, Logger

if TYPE_CHECKING:
    from pathlib import Path

    from anycorn.typing import HTTPScope, ResponseSummary


@pytest.mark.parametrize(
    ("target", "expected_name", "expected_handler_type"),
    [
        ("-", "anycorn.access", logging.StreamHandler),
        ("", "anycorn.access", logging.FileHandler),
        (logging.getLogger("test_special"), "test_special", None),
        (None, None, None),
    ],
)
def test_access_logger_init(
    target: logging.Logger | str | None,
    expected_name: str | None,
    expected_handler_type: type[logging.Handler] | None,
    tmp_path: Path,
) -> None:
    if target == "":
        target = str(tmp_path / "path")
    config = Config()
    config.accesslog = target
    config.access_log_format = "%h"
    logger = Logger(config)
    try:
        assert logger.access_log_format == "%h"
        assert logger.getEffectiveLevel() == logging.INFO
        if target is None:
            assert logger.access_logger is None
        else:
            assert logger.access_logger is not None
            assert logger.access_logger.name == expected_name
            if expected_handler_type is None:
                assert logger.access_logger.handlers == []
            else:
                assert isinstance(logger.access_logger.handlers[0], expected_handler_type)
    finally:
        # A file target opens a FileHandler; close it so the log file is not leaked.
        for candidate in (logger.access_logger, logger.error_logger):
            if candidate is not None:
                for handler in candidate.handlers:
                    handler.close()


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (logging.getLevelName(level_name), level_name)
        for level_name in range(logging.DEBUG, logging.CRITICAL + 1, 10)
    ],
)
def test_loglevel_option(level: str | None, expected: int) -> None:
    config = Config()
    assert level is not None
    config.loglevel = level
    logger = Logger(config)
    assert logger.error_logger is not None
    assert logger.error_logger.getEffectiveLevel() == expected


@pytest.fixture(name="response")
def _response_scope() -> dict:
    return {"status": 200, "headers": [(b"Content-Length", b"5"), (b"X-Anycorn", b"Anycorn")]}


def test_access_log_standard_atoms(http_scope: HTTPScope, response: ResponseSummary) -> None:
    atoms = AccessLogAtoms(http_scope, response, 0.000_023)
    assert atoms["h"] == "127.0.0.1:80"
    assert atoms["l"] == "-"
    assert time.strptime(atoms["t"], "[%d/%b/%Y:%H:%M:%S %z]")
    assert int(atoms["s"]) == 200  # noqa: PLR2004
    assert atoms["m"] == "GET"
    assert atoms["U"] == "/"
    assert atoms["q"] == "a=b"
    assert atoms["H"] == "2"
    assert int(atoms["b"]) == 5  # noqa: PLR2004
    assert int(atoms["B"]) == 5  # noqa: PLR2004
    assert atoms["f"] == "anycorn"
    assert atoms["a"] == "Anycorn"
    assert atoms["p"] == f"<{os.getpid()}>"
    assert atoms["not-atom"] == "-"
    assert int(atoms["T"]) == 0
    assert int(atoms["D"]) == 23  # noqa: PLR2004
    assert atoms["L"] == "0.000023"
    assert atoms["r"] == "GET / 2"
    assert atoms["R"] == "GET /?a=b 2"
    assert atoms["Uq"] == "/?a=b"
    assert atoms["st"] == "OK"


def test_access_log_header_atoms(http_scope: HTTPScope, response: ResponseSummary) -> None:
    atoms = AccessLogAtoms(http_scope, response, 0)
    assert atoms["{X-Anycorn}i"] == "Anycorn"
    assert atoms["{X-ANYCORN}i"] == "Anycorn"
    assert atoms["{not-atom}i"] == "-"
    assert atoms["{X-Anycorn}o"] == "Anycorn"
    assert atoms["{X-ANYCORN}o"] == "Anycorn"


def test_access_no_log_header_atoms(http_scope: HTTPScope) -> None:
    atoms = AccessLogAtoms(http_scope, {"status": 200, "headers": []}, 0)
    assert atoms["{X-Anycorn}i"] == "Anycorn"
    assert atoms["{X-ANYCORN}i"] == "Anycorn"
    assert atoms["{not-atom}i"] == "-"
    assert not any(key.startswith("{") and key.endswith("}o") for key in atoms)


def test_access_log_environ_atoms(http_scope: HTTPScope, response: ResponseSummary) -> None:
    os.environ["RANDOM"] = "Environ"
    atoms = AccessLogAtoms(http_scope, response, 0)
    assert atoms["{random}e"] == "Environ"


def test_nonstandard_status_code(http_scope: HTTPScope) -> None:
    atoms = AccessLogAtoms(http_scope, {"status": 441, "headers": []}, 0)
    assert atoms["st"] == "<???441???>"


def test_a_logger_created_without_a_level_is_still_wired_up() -> None:
    """A None loglevel must not be passed to setLevel, but the logger is still built."""
    config = Config()
    config.loglevel = None  # type: ignore[assignment]
    config.accesslog = "-"
    logger = Logger(config)
    assert logger.access_logger is not None
    assert isinstance(logger.access_logger.handlers[0], logging.StreamHandler)


@pytest.mark.anyio
async def test_error_helpers_write_through_to_the_error_logger() -> None:
    """Each helper forwards to the underlying error logger when one is configured."""
    config = Config()
    logger = Logger(config)
    logger.error_logger = Mock()
    await logger.critical("c")
    await logger.error("e")
    await logger.warning("w")
    await logger.info("i")
    await logger.debug("d")
    await logger.exception("x")
    await logger.log(logging.INFO, "l")
    logger.error_logger.critical.assert_called_once_with("c")
    logger.error_logger.error.assert_called_once_with("e")
    logger.error_logger.warning.assert_called_once_with("w")
    logger.error_logger.info.assert_called_once_with("i")
    logger.error_logger.debug.assert_called_once_with("d")
    logger.error_logger.exception.assert_called_once_with("x")
    logger.error_logger.log.assert_called_once_with(logging.INFO, "l")


@pytest.mark.anyio
async def test_error_helpers_are_no_ops_without_an_error_logger() -> None:
    """With no error log configured every helper is a silent no-op rather than a crash."""
    config = Config()
    config.errorlog = None
    logger = Logger(config)
    assert logger.error_logger is None
    await logger.critical("c")
    await logger.error("e")
    await logger.warning("w")
    await logger.info("i")
    await logger.debug("d")
    await logger.exception("x")
    await logger.log(logging.INFO, "l")


@pytest.mark.anyio
async def test_access_writes_atoms_when_an_access_logger_is_configured() -> None:
    config = Config()
    config.access_log_format = "%(h)s"
    logger = Logger(config)
    logger.access_logger = Mock()
    request: Any = {
        "type": "http",
        "headers": [],
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "client": ("127.0.0.1", 80),
    }
    await logger.access(request, {"status": 200, "headers": []}, 0.1)
    logger.access_logger.info.assert_called_once()


@pytest.mark.anyio
async def test_access_without_an_access_logger_is_a_no_op() -> None:
    config = Config()
    config.accesslog = None
    logger = Logger(config)
    assert logger.access_logger is None
    request: Any = {"type": "http"}
    await logger.access(request, {"status": 200, "headers": []}, 0.1)  # nothing to write to


def test_logconfig_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "log.json"
    path.write_text(json.dumps({"version": 1, "disable_existing_loggers": False}))
    config = Config()
    config.logconfig = f"json:{path}"
    Logger(config)  # applying the config must not raise


def test_logconfig_from_a_toml_file(tmp_path: Path) -> None:
    path = tmp_path / "log.toml"
    path.write_text("version = 1\ndisable_existing_loggers = false\n")
    config = Config()
    config.logconfig = f"toml:{path}"
    Logger(config)


def test_logconfig_from_an_ini_file(tmp_path: Path) -> None:
    path = tmp_path / "log.ini"
    path.write_text(
        "[loggers]\nkeys=root\n\n"
        "[handlers]\nkeys=console\n\n"
        "[formatters]\nkeys=simple\n\n"
        "[logger_root]\nlevel=INFO\nhandlers=console\n\n"
        "[handler_console]\nclass=StreamHandler\nlevel=INFO\nformatter=simple\n"
        "args=(sys.stdout,)\n\n"
        "[formatter_simple]\nformat=%(message)s\n"
    )
    config = Config()
    config.logconfig = str(path)
    Logger(config)


def test_logconfig_dict_is_applied() -> None:
    config = Config()
    config.logconfig_dict = {"version": 1, "disable_existing_loggers": False}
    Logger(config)


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (("127.0.0.1",), "127.0.0.1"),
        (("127.0.0.1", 80, "extra"), "<???('127.0.0.1', 80, 'extra')???>"),
    ],
)
def test_remote_addr_shapes(http_scope: HTTPScope, client: tuple, expected: str) -> None:
    """The remote address is derived from however many parts the client tuple has."""
    scope: Any = dict(http_scope)
    scope["client"] = client
    atoms = AccessLogAtoms(scope, {"status": 200, "headers": []}, 0)
    assert atoms["h"] == expected


def test_a_clientless_scope_has_no_remote_addr(http_scope: HTTPScope) -> None:
    scope: Any = dict(http_scope)
    scope["client"] = None
    atoms = AccessLogAtoms(scope, None, 0)  # response None: no response headers harvested
    assert atoms["h"] is None
    assert atoms["s"] == "-"
