"""OpsPilot Demo Lab sample application.

A small, self-contained service whose behaviour is driven entirely by its own
environment variables. The Demo Lab injects failures by changing those
variables, which produces a genuine ``UpdateFunctionConfiguration`` event in
CloudTrail - so change correlation is exercised against real AWS control-plane
activity rather than a simulation.

Custom metrics are emitted using CloudWatch Embedded Metric Format, so the
application's own error rate and latency are real CloudWatch metrics with no
extra API call in the request path.

This function has no dependency on the OpsPilot shared layer: it is the
*subject* of the platform, not part of it.
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "OpsPilot/DemoApp")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "opspilot-demo-app")

#: Configuration profiles the demo app understands. ``broken`` is what the
#: configuration_error scenario switches to.
CONFIG_PROFILES: dict[str, dict[str, Any]] = {
    "default": {"page_size": 25, "retries": 3, "region_suffix": "primary"},
    "broken": {"page_size": -1, "retries": 0, "region_suffix": ""},
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a structured log line."""
    print(json.dumps({"level": level, "service": SERVICE_NAME, "event": event, **fields},
                     default=str))


def _emit_metrics(
    latency_ms: float,
    http_errors: int = 0,
    db_throttles: int = 0,
    config_errors: int = 0,
) -> None:
    """Publish demo-app metrics via CloudWatch Embedded Metric Format.

    Values are emitted on every request (including zeros) so Sum-over-a-minute
    alarms have continuous, honest data instead of gaps.

    METRIC SEMANTICS - most specific signal wins.

    Each failure mode raises exactly ONE alarm, so one failure produces one
    incident rather than an alarm storm:

      * an unhandled crash        -> AWS/Lambda Errors (free, authoritative).
                                     No HttpErrors: the request never got a
                                     response to fail.
      * an invalid configuration  -> ConfigErrors only.
      * an injected 500 response  -> HttpErrors only.

    Emitting HttpErrors alongside the more specific signal would double-count
    the same failure and open two incidents for it.
    """
    document = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [["Service"]],
                    "Metrics": [
                        {"Name": "Requests", "Unit": "Count"},
                        {"Name": "HttpErrors", "Unit": "Count"},
                        {"Name": "RequestLatency", "Unit": "Milliseconds"},
                        {"Name": "DbThrottles", "Unit": "Count"},
                        {"Name": "ConfigErrors", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Service": SERVICE_NAME,
        "Requests": 1,
        "HttpErrors": http_errors,
        "RequestLatency": round(latency_ms, 2),
        "DbThrottles": db_throttles,
        "ConfigErrors": config_errors,
    }
    print(json.dumps(document))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Serve one demo request, behaving according to the injected fault mode."""
    started = time.time()
    failure_mode = _env("FAILURE_MODE", "none").lower()
    request_id = getattr(context, "aws_request_id", "local")

    db_throttles = 0
    config_errors = 0

    try:
        # --- Configuration failure -------------------------------------------
        profile_name = _env("CONFIG_PROFILE", "default")
        profile = CONFIG_PROFILES.get(profile_name)
        if profile is None or profile.get("page_size", 0) <= 0:
            config_errors = 1
            _log("ERROR", "configuration_invalid", profile=profile_name,
                 detail="page_size must be positive")
            _emit_metrics((time.time() - started) * 1000, config_errors=1)
            return _response(
                500,
                {
                    "error": "configuration_error",
                    "message": f"Invalid configuration profile '{profile_name}'",
                    "request_id": request_id,
                },
            )

        # --- Latency injection ------------------------------------------------
        latency_ms = _env_int("LATENCY_MS", 0)
        if latency_ms > 0:
            capped = min(latency_ms, 20_000)
            _log("WARNING", "artificial_latency_applied", latency_ms=capped)
            time.sleep(capped / 1000.0)

        # --- Database work (and the throttle scenario) ------------------------
        write_burst = _env_int("WRITE_BURST", 0)
        if write_burst > 0:
            db_throttles = _drive_table_load(write_burst)

        # --- Unhandled Lambda error ------------------------------------------
        if failure_mode == "error":
            _log("ERROR", "injected_lambda_failure", failure_mode=failure_mode)
            _emit_metrics((time.time() - started) * 1000, db_throttles=db_throttles)
            raise RuntimeError(
                "Demo Lab injected failure: downstream dependency returned an "
                "unrecoverable error"
            )

        # --- Handled application error (HTTP 500) -----------------------------
        error_rate = _env_float("ERROR_RATE", 0.0)
        if error_rate > 0 and random.random() < error_rate:
            _log("ERROR", "injected_application_error", error_rate=error_rate)
            _emit_metrics((time.time() - started) * 1000, http_errors=1,
                          db_throttles=db_throttles)
            return _response(
                500,
                {
                    "error": "internal_error",
                    "message": "Demo Lab injected application error",
                    "request_id": request_id,
                },
            )

        elapsed_ms = (time.time() - started) * 1000
        _emit_metrics(elapsed_ms, db_throttles=db_throttles)
        _log("INFO", "request_served", duration_ms=round(elapsed_ms, 2),
             failure_mode=failure_mode, db_throttles=db_throttles)

        return _response(
            200,
            {
                "service": SERVICE_NAME,
                "status": "healthy",
                "config_profile": profile_name,
                "page_size": profile["page_size"],
                "duration_ms": round(elapsed_ms, 2),
                "db_throttles": db_throttles,
                "request_id": request_id,
            },
        )

    except RuntimeError:
        raise  # injected fault: must surface as a real Lambda error metric
    except Exception as exc:  # noqa: BLE001 - unexpected faults still count
        _log("ERROR", "unhandled_exception", error_type=type(exc).__name__,
             error=str(exc)[:300])
        _emit_metrics((time.time() - started) * 1000, db_throttles=db_throttles,
                      config_errors=config_errors)
        raise


def _drive_table_load(operations: int) -> int:
    """Write to the demo table hard enough to exceed its provisioned capacity.

    The demo table is deliberately provisioned at 1 WCU (inside the free tier),
    so a modest burst produces genuine DynamoDB throttling rather than a
    simulated metric.

    Writes go through ``batch_write_item`` in batches of 25 rather than one at a
    time. That matters: 40 sequential PutItem calls take ~3 seconds, which would
    also breach the demo's *latency* alarm and open a second incident for the
    same failure. Batching completes in a couple of round trips, so this
    scenario raises exactly one alarm - the throttling one.

    Throttled writes come back in ``UnprocessedItems``. They are deliberately
    NOT retried: the throttling must reach CloudWatch, not be absorbed.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    table_name = _env("TARGET_TABLE")
    if not table_name:
        _log("WARNING", "write_burst_skipped", reason="no target table configured")
        return 0

    # No client-side retries, for the same reason.
    client = boto3.client(
        "dynamodb",
        config=Config(retries={"max_attempts": 0, "mode": "standard"}, read_timeout=5),
    )

    payload = "x" * 3800  # keep each write near 4 KB = 1 WCU
    expires_at = str(int(time.time()) + 3600)
    throttled = 0
    attempted = 0
    remaining = min(operations, 50)
    batch = 0

    while remaining > 0:
        size = min(25, remaining)  # batch_write_item accepts at most 25 items
        items = [
            {
                "PutRequest": {
                    "Item": {
                        "pk": {"S": f"load-{(batch * 25 + index) % 10}"},
                        "sk": {"S": f"{time.time_ns()}-{index}"},
                        "payload": {"S": payload},
                        "ttl": {"N": expires_at},
                    }
                }
            }
            for index in range(size)
        ]
        attempted += size
        try:
            response = client.batch_write_item(RequestItems={table_name: items})
            throttled += len(response.get("UnprocessedItems", {}).get(table_name, []))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {
                "ProvisionedThroughputExceededException",
                "ThrottlingException",
                "RequestLimitExceeded",
            }:
                throttled += size  # the whole batch was rejected
            else:
                _log("WARNING", "write_burst_error", error_code=code)
                break
        except Exception as exc:  # noqa: BLE001
            _log("WARNING", "write_burst_failed", error_type=type(exc).__name__)
            break

        remaining -= size
        batch += 1

    if throttled:
        _log(
            "ERROR",
            "dynamodb_throttling_detected",
            throttled_writes=throttled,
            attempted=attempted,
            table=table_name,
        )
    return throttled


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }
