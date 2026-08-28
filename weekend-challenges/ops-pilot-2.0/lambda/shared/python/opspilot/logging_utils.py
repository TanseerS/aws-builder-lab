"""Structured JSON logging for OpsPilot.

Every log line is a single JSON object so CloudWatch Logs Insights can query
OpsPilot's own behaviour. Nothing here ever logs credentials or raw payloads.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

_REDACT_KEYS = {
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "credentials",
    "password",
    "secret",
    "token",
    "x-api-key",
}

_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _scrub(value: Any) -> Any:
    """Recursively redact values whose key looks credential-bearing."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if k.lower() in _REDACT_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class StructuredLogger:
    """Minimal JSON logger bound to one OpsPilot component."""

    def __init__(self, service: str) -> None:
        self.service = service
        self._context: dict[str, Any] = {}
        self._logger = logging.getLogger(service)
        self._logger.setLevel(getattr(logging, _LEVEL, logging.INFO))
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._logger.propagate = False

    def bind(self, **kwargs: Any) -> None:
        """Attach fields (e.g. incident_id) to every subsequent log line."""
        self._context.update({k: v for k, v in kwargs.items() if v is not None})

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "level": level,
            "service": self.service,
            "event": event,
            **self._context,
            **_scrub(fields),
        }
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"level": level, "service": self.service, "event": event})
        self._logger.log(getattr(logging, level, logging.INFO), line)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("DEBUG", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("ERROR", event, **fields)

    @contextmanager
    def timed(self, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Time a block and log its duration, whether or not it raises."""
        started = time.time()
        extra: dict[str, Any] = {}
        try:
            yield extra
        except Exception as exc:  # noqa: BLE001 - deliberately logged and re-raised
            self.error(
                f"{event}_failed",
                duration_ms=int((time.time() - started) * 1000),
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                **fields,
                **extra,
            )
            raise
        else:
            self.info(
                f"{event}_completed",
                duration_ms=int((time.time() - started) * 1000),
                **fields,
                **extra,
            )


def get_logger(service: str) -> StructuredLogger:
    """Return a structured logger for a named OpsPilot component."""
    return StructuredLogger(service)
