from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from anycorn.config import Config
from anycorn.logging import AccessLogAtoms, Logger
from anycorn.typing import HTTPScope, ResponseSummary


@pytest.mark.parametrize(
    "target, expected_name, expected_handler_type",
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
    assert logger.access_log_format == "%h"
    assert logger.getEffectiveLevel() == logging.INFO
    if target is None:
        assert logger.access_logger is None
    elif expected_name is None:
        assert logger.access_logger.handlers == []
    else:
        assert logger.access_logger.name == expected_name
        if expected_handler_type is None:
            assert logger.access_logger.handlers == []
        else:
            assert isinstance(logger.access_logger.handlers[0], expected_handler_type)


@pytest.mark.parametrize(
    "level, expected",
    [
        (logging.getLevelName(level_name), level_name)
        for level_name in range(logging.DEBUG, logging.CRITICAL + 1, 10)
    ],
)
def test_loglevel_option(level: str | None, expected: int) -> None:
    config = Config()
    config.loglevel = level
    logger = Logger(config)
    assert logger.error_logger.getEffectiveLevel() == expected


@pytest.fixture(name="response")
def _response_scope() -> dict:
    return {"status": 200, "headers": [(b"Content-Length", b"5"), (b"X-Anycorn", b"Anycorn")]}


def test_access_log_standard_atoms(http_scope: HTTPScope, response: ResponseSummary) -> None:
    atoms = AccessLogAtoms(http_scope, response, 0.000_023)
    assert atoms["h"] == "127.0.0.1:80"
    assert atoms["l"] == "-"
    assert time.strptime(atoms["t"], "[%d/%b/%Y:%H:%M:%S %z]")
    assert int(atoms["s"]) == 200
    assert atoms["m"] == "GET"
    assert atoms["U"] == "/"
    assert atoms["q"] == "a=b"
    assert atoms["H"] == "2"
    assert int(atoms["b"]) == 5
    assert int(atoms["B"]) == 5
    assert atoms["f"] == "anycorn"
    assert atoms["a"] == "Anycorn"
    assert atoms["p"] == f"<{os.getpid()}>"
    assert atoms["not-atom"] == "-"
    assert int(atoms["T"]) == 0
    assert int(atoms["D"]) == 23
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
    assert not any(key.startswith("{") and key.endswith("}o") for key in atoms.keys())


def test_access_log_environ_atoms(http_scope: HTTPScope, response: ResponseSummary) -> None:
    os.environ["Random"] = "Environ"
    atoms = AccessLogAtoms(http_scope, response, 0)
    assert atoms["{random}e"] == "Environ"


def test_nonstandard_status_code(http_scope: HTTPScope) -> None:
    atoms = AccessLogAtoms(http_scope, {"status": 441, "headers": []}, 0)
    assert atoms["st"] == "<???441???>"
