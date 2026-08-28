"""Consistent HTTP responses for the API Lambda.

Every OpsPilot endpoint returns the same JSON envelope and honest status codes,
so the dashboard has exactly one shape to handle.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    **_CORS_HEADERS,
}


class _Encoder(json.JSONEncoder):
    """JSON encoder that understands DynamoDB Decimals."""

    def default(self, o: Any) -> Any:  # noqa: D102
        if isinstance(o, Decimal):
            as_float = float(o)
            return int(as_float) if as_float.is_integer() else as_float
        if isinstance(o, set):
            return sorted(o)
        return str(o)


def respond(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": dict(_BASE_HEADERS),
        "body": json.dumps(body, cls=_Encoder),
    }


def ok(data: Any, **extra: Any) -> dict[str, Any]:
    """200 with a successful payload."""
    return respond(200, {"ok": True, "data": data, **extra})


def accepted(data: Any, **extra: Any) -> dict[str, Any]:
    """202 for work that has been handed off asynchronously."""
    return respond(202, {"ok": True, "data": data, **extra})


def error(status_code: int, message: str, code: str = "", **extra: Any) -> dict[str, Any]:
    """Non-2xx with a machine-readable error code and human message."""
    return respond(
        status_code,
        {"ok": False, "error": {"code": code or _DEFAULT_CODES.get(status_code, "error"),
                                "message": message}, **extra},
    )


def bad_request(message: str, code: str = "bad_request") -> dict[str, Any]:
    return error(400, message, code)


def not_found(message: str = "Resource not found") -> dict[str, Any]:
    return error(404, message, "not_found")


def conflict(message: str, code: str = "conflict") -> dict[str, Any]:
    return error(409, message, code)


def server_error(message: str = "Internal server error") -> dict[str, Any]:
    return error(500, message, "internal_error")


_DEFAULT_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    500: "internal_error",
    503: "service_unavailable",
}
