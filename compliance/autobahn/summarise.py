"""Summarise Autobahn WebSocket compliance test results."""

import json
import pathlib
import sys

with pathlib.Path("reports/servers/index.json").open() as file_:
    report = json.load(file_)

failures = sum(value["behavior"] == "FAILED" for value in report["websockets"].values())

if failures > 0:
    sys.exit(1)
else:
    sys.exit(0)
