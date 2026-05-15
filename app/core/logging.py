"""Logging setup.

The application supports two output formats:

* ``plain`` (default): human-friendly single-line text, useful in a TTY.
* ``json``: one JSON object per line, suitable for log aggregators
  (Datadog, Loki, CloudWatch, etc.).

Both formats include the current request id (see
:mod:`app.core.request_context`) so a single API call can be followed
across the synchronous and background-worker code paths.
"""

from __future__ import annotations

import json
import logging
import sys

from app.core.request_context import get_request_id


class _RequestIdFilter(logging.Filter):
    """Inject the current ``request_id`` onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """Render log records as compact JSON, one record per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", get_request_id()),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Install a single stdout handler on the root logger (idempotent)."""
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
