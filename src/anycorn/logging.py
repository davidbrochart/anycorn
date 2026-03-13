"""Logging utilities for Anycorn, including access and error log formatting."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
from http import HTTPStatus
from logging.config import dictConfig, fileConfig
from typing import IO, TYPE_CHECKING, Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import Config
    from .typing import ResponseSummary, WWWScope


def _create_logger(
    name: str,
    target: logging.Logger | str | None,
    level: str | None,
    sys_default: IO,
    *,
    propagate: bool = True,
) -> logging.Logger | None:
    if isinstance(target, logging.Logger):
        return target

    if target:
        logger = logging.getLogger(name)
        logger.handlers = [
            logging.StreamHandler(sys_default) if target == "-" else logging.FileHandler(target)
        ]
        logger.propagate = propagate
        formatter = logging.Formatter(
            "%(asctime)s [%(process)d] [%(levelname)s] %(message)s",
            "[%Y-%m-%d %H:%M:%S %z]",
        )
        logger.handlers[0].setFormatter(formatter)
        if level is not None:
            logger.setLevel(logging.getLevelName(level.upper()))
        return logger
    return None


class Logger:
    """Anycorn logger providing access and error logging."""

    def __init__(self, config: Config) -> None:
        self.access_log_format = config.access_log_format

        self.access_logger = _create_logger(
            "anycorn.access",
            config.accesslog,
            config.loglevel,
            sys.stdout,
            propagate=False,
        )
        self.error_logger = _create_logger(
            "anycorn.error", config.errorlog, config.loglevel, sys.stderr
        )

        if config.logconfig is not None:
            if config.logconfig.startswith("json:"):
                with pathlib.Path(config.logconfig[5:]).open() as file_:
                    dictConfig(json.load(file_))
            elif config.logconfig.startswith("toml:"):
                with pathlib.Path(config.logconfig[5:]).open("rb") as file_:
                    dictConfig(tomllib.load(file_))
            else:
                log_config = {
                    "__file__": config.logconfig,
                    "here": str(pathlib.Path(config.logconfig).parent),
                }
                fileConfig(config.logconfig, defaults=log_config, disable_existing_loggers=False)
        elif config.logconfig_dict is not None:
            dictConfig(config.logconfig_dict)

    async def access(
        self, request: WWWScope, response: ResponseSummary | None, request_time: float
    ) -> None:
        """Log an access log entry."""
        if self.access_logger is not None:
            self.access_logger.info(
                self.access_log_format, self.atoms(request, response, request_time)
            )

    async def critical(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a critical-level message."""
        if self.error_logger is not None:
            self.error_logger.critical(message, *args, **kwargs)

    async def error(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an error-level message."""
        if self.error_logger is not None:
            self.error_logger.error(message, *args, **kwargs)

    async def warning(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a warning-level message."""
        if self.error_logger is not None:
            self.error_logger.warning(message, *args, **kwargs)

    async def info(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an info-level message."""
        if self.error_logger is not None:
            self.error_logger.info(message, *args, **kwargs)

    async def debug(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a debug-level message."""
        if self.error_logger is not None:
            self.error_logger.debug(message, *args, **kwargs)

    async def exception(self, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log an exception with traceback."""
        if self.error_logger is not None:
            self.error_logger.exception(message, *args, **kwargs)

    async def log(self, level: int, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Log a message at the given numeric level."""
        if self.error_logger is not None:
            self.error_logger.log(level, message, *args, **kwargs)

    def atoms(
        self, request: WWWScope, response: ResponseSummary | None, request_time: float
    ) -> Mapping[str, str]:
        """Create and return an access log atoms dictionary.

        This can be overidden and customised if desired. It should
        return a mapping between an access log format key and a value.
        """
        return AccessLogAtoms(request, response, request_time)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self.error_logger, name)


class AccessLogAtoms(dict):
    """Dictionary of atoms for formatting access log entries."""

    def __init__(
        self, request: WWWScope, response: ResponseSummary | None, request_time: float
    ) -> None:
        super().__init__()
        for name, value in request["headers"]:
            self[f"{{{name.decode('latin1').lower()}}}i"] = value.decode("latin1")
        for name, value in os.environ.items():
            self[f"{{{name.lower()}}}e"] = value
        protocol = request.get("http_version", "ws")
        client = request.get("client")
        if client is None:
            remote_addr = None
        elif len(client) == 2:  # noqa: PLR2004
            remote_addr = f"{client[0]}:{client[1]}"
        elif len(client) == 1:
            remote_addr = client[0]
        else:  # make sure not to throw UnboundLocalError
            remote_addr = f"<???{client}???>"
        method = request["method"] if request["type"] == "http" else "GET"
        query_string = request["query_string"].decode()
        path_with_qs = request["path"] + ("?" + query_string if query_string else "")

        status_code = "-"
        status_phrase = "-"
        if response is not None:
            for name, value in response.get("headers", []):
                self[f"{{{name.decode('latin1').lower()}}}o"] = value.decode("latin1")
            status_code = str(response["status"])
            try:
                status_phrase = HTTPStatus(response["status"]).phrase
            except ValueError:
                status_phrase = f"<???{status_code}???>"
        self.update(
            {
                "h": remote_addr,
                "l": "-",
                "t": time.strftime("[%d/%b/%Y:%H:%M:%S %z]"),
                "r": f"{method} {request['path']} {protocol}",
                "R": f"{method} {path_with_qs} {protocol}",
                "s": status_code,
                "st": status_phrase,
                "S": request["scheme"],
                "m": method,
                "U": request["path"],
                "Uq": path_with_qs,
                "q": query_string,
                "H": protocol,
                "b": self["{Content-Length}o"],
                "B": self["{Content-Length}o"],
                "f": self["{Referer}i"],
                "a": self["{User-Agent}i"],
                "T": int(request_time),
                "D": int(request_time * 1_000_000),
                "L": f"{request_time:.6f}",
                "p": f"<{os.getpid()}>",
            }
        )

    def __getitem__(self, key: str) -> str:
        """Return the atom for *key*, or ``'-'`` if missing."""
        try:
            if key.startswith("{"):
                return super().__getitem__(key.lower())
            return super().__getitem__(key)
        except KeyError:
            return "-"
