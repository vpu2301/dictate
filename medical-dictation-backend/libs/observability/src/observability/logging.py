"""Structured logging setup.

JSON output to stdout. Every log line carries:

* ``timestamp`` — ISO 8601 UTC
* ``level``, ``logger``, ``message``
* ``service`` — injected at setup time
* ``trace_id``, ``span_id`` — populated from the active OTel span
* PII / secret filter applied last so nothing in the drop list reaches stdout
"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import json as jsonlogger

from .correlation import CorrelationIdFilter
from .pii_filter import PIISafeFilter


def setup_logging(service_name: str, log_level: str = "INFO") -> None:
    """Configure the root logger with JSON output, correlation IDs, PII filtering.

    Idempotent — safe to call multiple times; only installs handlers once.
    """
    root = logging.getLogger()
    root.setLevel(log_level.upper())
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s %(trace_id)s %(span_id)s",
        datefmt="%Y-%m-%dT%H:%M:%S.%fZ",
        rename_fields={"levelname": "level", "asctime": "timestamp", "name": "logger"},
        static_fields={"service": service_name},
    )
    handler.setFormatter(formatter)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(PIISafeFilter())
    root.addHandler(handler)
