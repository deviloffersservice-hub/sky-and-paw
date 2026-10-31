"""Structured logging: one JSON object per line on stderr.

Use ``get_logger(__name__)`` everywhere instead of ``print``. Extra structured
fields go in ``extra={"fields": {...}}`` or through ``log_event``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "fields"}
# Noise added by uvicorn's loggers (ANSI-coloured duplicate message, protocol object repr).
_DROPPED = {"color_message", "websocket"}
_configured = False


class JsonFormatter(logging.Formatter):
    """Format a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in _DROPPED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | int = "INFO") -> None:
    """Install the JSON handler on the root logger and route uvicorn's loggers to it.

    Idempotent: calling it again only updates the level.
    """
    global _configured
    root = logging.getLogger()
    root.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]
        _configured = True
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log ``event`` with structured ``fields`` as top-level JSON keys."""
    logger.log(level, event, extra={"fields": fields})


__all__ = ["JsonFormatter", "configure_logging", "get_logger", "log_event"]
