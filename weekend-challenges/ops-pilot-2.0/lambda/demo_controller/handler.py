"""Demo Lab controller - injects and clears controlled failures.

Failure injection works by rewriting the demo function's environment
variables. That is a real ``lambda:UpdateFunctionConfiguration`` call, so it
appears in CloudTrail exactly as a production deployment would, and OpsPilot's
change correlation has genuine control-plane activity to find.

The safety boundary is enforced twice: this function's IAM role can only touch
the demo function, and the code refuses any target outside the Demo Lab prefix.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from opspilot import config, dynamo, models
from opspilot.aws_clients import client
from opspilot.logging_utils import get_logger

log = get_logger("demo_controller")

BASELINE_ENV: dict[str, str] = json.loads(os.environ.get("DEMO_BASELINE_ENV", "{}"))
MUTABLE_ENV_KEYS: frozenset[str] = frozenset(
    json.loads(os.environ.get("DEMO_MUTABLE_ENV_KEYS", "[]"))
)
DEMO_ALARMS: list[str] = json.loads(os.environ.get("DEMO_ALARMS", "[]"))
#: Invocations fired after injection so the failure reaches CloudWatch promptly.
INJECT_TRAFFIC = int(os.environ.get("INJECT_TRAFFIC", "8"))


class Scenario:
    """The supported controlled failure modes."""

    LAMBDA_ERROR = "lambda_error"
    LAMBDA_LATENCY = "lambda_latency"
    APPLICATION_ERROR = "application_error"
    DATABASE_THROTTLE = "database_throttle"
    CONFIGURATION_ERROR = "configuration_error"


#: Environment overrides applied for each scenario, plus what to tell the user.
SCENARIOS: dict[str, dict[str, Any]] = {
    Scenario.LAMBDA_ERROR: {
        "env": {"FAILURE_MODE": "error"},
        "title": "Lambda error injection",
        "description": "The demo function raises an unhandled exception on every request.",
        "expected_alarm_seconds": 150,
    },
    Scenario.LAMBDA_LATENCY: {
        "env": {"FAILURE_MODE": "latency", "LATENCY_MS": "4000"},
        "title": "Lambda latency injection",
        "description": "The demo function sleeps 4s per request, breaching the duration alarm.",
        "expected_alarm_seconds": 180,
    },
    Scenario.APPLICATION_ERROR: {
        "env": {"FAILURE_MODE": "application_error", "ERROR_RATE": "0.8"},
        "title": "Application error spike",
        "description": "The demo application returns HTTP 500 for roughly 80% of requests.",
        "expected_alarm_seconds": 180,
    },
    Scenario.DATABASE_THROTTLE: {
        "env": {"FAILURE_MODE": "database_throttle", "WRITE_BURST": "40"},
        "title": "DynamoDB throttling",
        "description": (
            "The demo application bursts writes past the demo table's 1 WCU "
            "provisioned capacity, producing real DynamoDB throttling."
        ),
        "expected_alarm_seconds": 180,
    },
    Scenario.CONFIGURATION_ERROR: {
        "env": {"FAILURE_MODE": "configuration_error", "CONFIG_PROFILE": "broken"},
        "title": "Configuration failure",
        "description": (
            "The demo application is switched to an invalid configuration "
            "profile and rejects every request."
        ),
        "expected_alarm_seconds": 180,
    },
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route a Demo Lab operation: inject, reset or status."""
    operation = event.get("operation", "status")
    log.bind(operation=operation, request_id=getattr(context, "aws_request_id", None))

    try:
        _assert_demo_target()
        if operation == "inject":
            return _inject(str(event.get("scenario", "")).strip().lower())
        if operation == "reset":
            return _reset()
        if operation == "status":
            return _status()
        return {"ok": False, "error": f"Unknown operation '{operation}'"}
    except (ClientError, BotoCoreError) as exc:
        log.error("demo_operation_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        return {"ok": False, "error": f"AWS error: {type(exc).__name__}"}


def _assert_demo_target() -> None:
    """Refuse to operate on anything outside the OpsPilot Demo Lab."""
    if not config.DEMO_FUNCTION_NAME:
        raise RuntimeError("No demo function configured")
    if not config.DEMO_FUNCTION_NAME.startswith(config.RESOURCE_PREFIX):
        raise RuntimeError("Demo target is outside the OpsPilot Demo Lab")


# --- Operations ---------------------------------------------------------------
def _inject(scenario: str) -> dict[str, Any]:
    """Apply one failure scenario to the demo application."""
    spec = SCENARIOS.get(scenario)
    if spec is None:
        return {
            "ok": False,
            "error": f"Unknown scenario '{scenario}'",
            "supported": sorted(SCENARIOS),
        }

    previous = _read_env()
    overrides = {**{key: BASELINE_ENV.get(key, "") for key in MUTABLE_ENV_KEYS}, **spec["env"]}
    applied = _write_env(overrides)

    change_id = f"demo-{scenario}-{uuid.uuid4().hex[:8]}"
    dynamo.record_change(
        {
            "change_id": change_id,
            "service": "lambda",
            "resource": config.DEMO_FUNCTION_NAME,
            "action": "fault_injection",
            "actor": "opspilot-demo-lab",
            "details": f"Injected {scenario}: {json.dumps(spec['env'])}",
            "scenario": scenario,
            # Snapshot for restore_previous_demo_version.
            "previous_environment": {
                k: v for k, v in previous.items() if k in MUTABLE_ENV_KEYS
            },
        }
    )

    invoked = _drive_traffic(INJECT_TRAFFIC)
    log.info("scenario_injected", scenario=scenario, invocations=invoked)

    return {
        "ok": True,
        "scenario": scenario,
        "title": spec["title"],
        "description": spec["description"],
        "applied": applied,
        "change_id": change_id,
        "invocations_triggered": invoked,
        "expected_alarm_seconds": spec["expected_alarm_seconds"],
        "message": (
            f"{spec['title']} injected. The CloudWatch alarm typically fires within "
            f"{spec['expected_alarm_seconds'] // 60}-{spec['expected_alarm_seconds'] // 60 + 1} "
            "minutes, then OpsPilot opens and investigates the incident automatically."
        ),
    }


def _reset() -> dict[str, Any]:
    """Restore the demo environment to its Terraform-managed healthy state."""
    previous = _read_env()
    applied = _write_env({key: BASELINE_ENV.get(key, "") for key in MUTABLE_ENV_KEYS})

    dynamo.record_change(
        {
            "change_id": f"demo-reset-{uuid.uuid4().hex[:8]}",
            "service": "lambda",
            "resource": config.DEMO_FUNCTION_NAME,
            # Distinct from a generic configuration_change so change
            # correlation can recognise it as restorative.
            "action": "configuration_reset",
            "actor": "opspilot-demo-lab",
            "details": "Demo environment reset to Terraform baseline",
            "previous_environment": {
                k: v for k, v in previous.items() if k in MUTABLE_ENV_KEYS
            },
        }
    )

    invoked = _drive_traffic(3)
    log.info("environment_reset", invocations=invoked)
    return {
        "ok": True,
        "applied": applied,
        "invocations_triggered": invoked,
        "message": (
            "Demo environment reset to healthy. Alarms return to OK once "
            "CloudWatch evaluates the next healthy period."
        ),
    }


def _status() -> dict[str, Any]:
    """Report the demo environment's live configuration and alarm states."""
    env_vars = _read_env()
    active = env_vars.get("FAILURE_MODE", "") or "none"

    alarm_states: dict[str, str] = {}
    if DEMO_ALARMS:
        try:
            response = client("cloudwatch").describe_alarms(AlarmNames=DEMO_ALARMS)
            alarm_states = {
                a.get("AlarmName", ""): a.get("StateValue", "")
                for a in response.get("MetricAlarms", [])
            }
        except (ClientError, BotoCoreError) as exc:
            log.warning("alarm_states_unavailable", error=str(exc)[:200])

    firing = [name for name, state in alarm_states.items() if state == "ALARM"]
    return {
        "ok": True,
        "healthy": active == "none" and not firing,
        "active_scenario": active if active != "none" else None,
        "function": config.DEMO_FUNCTION_NAME,
        "configuration": {k: v for k, v in env_vars.items() if k in MUTABLE_ENV_KEYS},
        "alarms": alarm_states,
        "alarms_firing": firing,
        "scenarios": [
            {"scenario": key, "title": spec["title"], "description": spec["description"]}
            for key, spec in SCENARIOS.items()
        ],
        "checked_at": models.iso(),
    }


# --- Demo function mutation ---------------------------------------------------
def _read_env() -> dict[str, str]:
    """Read the demo function's current environment variables."""
    response = client("lambda").get_function_configuration(
        FunctionName=config.DEMO_FUNCTION_NAME
    )
    return (response.get("Environment") or {}).get("Variables", {}) or {}


def _write_env(overrides: dict[str, str]) -> dict[str, str]:
    """Write the demo function's environment, restricted to mutable keys."""
    current = _read_env()
    desired = dict(current)
    applied: dict[str, str] = {}

    for key, value in overrides.items():
        if key not in MUTABLE_ENV_KEYS:
            log.warning("env_key_refused", key=key)
            continue
        desired[key] = str(value)
        applied[key] = str(value)

    if applied:
        client("lambda").update_function_configuration(
            FunctionName=config.DEMO_FUNCTION_NAME,
            Environment={"Variables": desired},
        )
        _wait_for_update()
    return applied


def _wait_for_update(attempts: int = 12, delay: float = 1.0) -> None:
    """Block until the demo function's pending update has settled.

    Bounded at ~12s so the whole operation fits inside the API Lambda's 29s
    budget when it calls this synchronously. If the update is still pending we
    log and continue rather than failing: the write itself already succeeded.
    """
    import time

    for _ in range(attempts):
        response = client("lambda").get_function_configuration(
            FunctionName=config.DEMO_FUNCTION_NAME
        )
        if response.get("LastUpdateStatus") != "InProgress":
            return
        time.sleep(delay)
    log.warning("function_update_still_in_progress")


def _drive_traffic(invocations: int) -> int:
    """Invoke the demo app so the new behaviour reaches CloudWatch quickly.

    Without this, the first datapoint would wait on the one-minute scheduled
    traffic generator and the demo would feel unresponsive.
    """
    payload = json.dumps(
        {"rawPath": "/demo/app", "requestContext": {"http": {"method": "GET"}}}
    ).encode("utf-8")
    succeeded = 0
    for _ in range(max(0, invocations)):
        try:
            client("lambda").invoke(
                FunctionName=config.DEMO_FUNCTION_NAME,
                InvocationType="Event",  # async: errors still count in metrics
                Payload=payload,
            )
            succeeded += 1
        except (ClientError, BotoCoreError) as exc:
            log.warning("traffic_invocation_failed", error=str(exc)[:200])
            break
    return succeeded
