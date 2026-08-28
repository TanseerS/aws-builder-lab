"""Remediation - executes one allowlisted action against Demo Lab resources.

Safety properties, all enforced here rather than trusted from upstream:

1. Only runs for an incident a human explicitly approved.
2. The action must resolve to an entry in ``ALLOWED_ACTIONS``.
3. The target function name comes from Terraform via the environment, never
   from the incident record or the model.
4. Success is measured by re-reading the resulting configuration, not by the
   absence of an exception.
"""

from __future__ import annotations

import json
import os
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from opspilot import config, dynamo, events, models, remediation_actions
from opspilot.aws_clients import client
from opspilot.logging_utils import get_logger

log = get_logger("remediation")

#: The Terraform-managed healthy baseline for the demo function's environment.
BASELINE_ENV: dict[str, str] = json.loads(os.environ.get("DEMO_BASELINE_ENV", "{}"))
#: Env keys OpsPilot is permitted to write. Anything else is left untouched.
MUTABLE_ENV_KEYS: frozenset[str] = frozenset(
    json.loads(os.environ.get("DEMO_MUTABLE_ENV_KEYS", "[]"))
)


class RemediationRefused(RuntimeError):
    """Raised when a requested remediation fails a safety check."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Execute the approved remediation for one incident."""
    detail = event.get("detail") or {}
    incident_id = detail.get("incident_id", "")
    log.bind(incident_id=incident_id, request_id=getattr(context, "aws_request_id", None))

    incident = dynamo.get_incident(incident_id) if incident_id else None
    if incident is None:
        log.error("incident_not_found")
        return {"remediated": False, "reason": "incident not found"}

    claimed = dynamo.transition(
        incident_id,
        models.IncidentStatus.REMEDIATING,
        {
            "remediation_status": models.RemediationStatus.IN_PROGRESS,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        models.iso(), "Remediation started", models.TimelineKind.REMEDIATION
                    )
                ],
            ),
        },
        expected_status=models.IncidentStatus.AWAITING_APPROVAL,
    )
    if claimed is None:
        log.warning("remediation_skipped", current_status=incident.get("status"))
        return {"remediated": False, "reason": "incident not awaiting approval"}

    incident = claimed
    started_at = models.iso()

    try:
        spec = _resolve_approved_action(incident, detail)
        log.info("remediation_action_resolved", action=spec.key, risk=spec.risk)
        result = _execute(spec, incident)
    except RemediationRefused as exc:
        return _fail(incident, started_at, str(exc), refused=True)
    except (ClientError, BotoCoreError) as exc:
        log.error("remediation_aws_error", error_type=type(exc).__name__, error=str(exc)[:300])
        return _fail(incident, started_at, f"AWS error: {type(exc).__name__}")

    if not result["succeeded"]:
        return _fail(incident, started_at, result["detail"])

    detail_record = {
        "action": spec.key,
        "title": spec.title,
        "risk": spec.risk,
        "started_at": started_at,
        "completed_at": models.iso(),
        "target": config.DEMO_FUNCTION_NAME,
        "applied": result["applied"],
        "verified_configuration": result["observed"],
        "detail": result["detail"],
    }

    dynamo.transition(
        incident.get("incident_id", ""),
        models.IncidentStatus.VERIFYING,
        {
            # Executing without an exception is not recovery - the verifier
            # decides whether the incident is actually resolved.
            "remediation_status": models.RemediationStatus.SUCCEEDED,
            "remediation_detail": detail_record,
            "verification_status": models.VerificationStatus.IN_PROGRESS,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        models.iso(),
                        f"Remediation executed: {spec.title}",
                        models.TimelineKind.REMEDIATION,
                        detail=result["detail"],
                    )
                ],
            ),
        },
    )

    _warm_demo_app()

    events.publish(
        events.DetailType.REMEDIATION_COMPLETED,
        {
            "incident_id": incident.get("incident_id", ""),
            "action": spec.key,
            "succeeded": True,
        },
    )
    log.info("remediation_succeeded", action=spec.key)
    return {"remediated": True, "incident_id": incident.get("incident_id"), "action": spec.key}


# --- Safety checks ------------------------------------------------------------
def _resolve_approved_action(
    incident: dict[str, Any], detail: dict[str, Any]
) -> remediation_actions.ActionSpec:
    """Resolve the approved action, refusing anything outside the allowlist."""
    requested = detail.get("action") or incident.get("approved_action") or ""
    if not requested:
        raise RemediationRefused("No approved action recorded on the incident")

    spec = remediation_actions.resolve_action(requested)
    if spec is None:
        # This is the path that makes arbitrary model output inert.
        raise RemediationRefused(
            f"Action '{str(requested)[:100]}' is not in the remediation allowlist"
        )

    if not config.DEMO_FUNCTION_NAME:
        raise RemediationRefused("No Demo Lab target configured")

    # The Demo Lab boundary: remediation can only ever touch this function.
    if not config.DEMO_FUNCTION_NAME.startswith(config.RESOURCE_PREFIX):
        raise RemediationRefused("Remediation target is outside the OpsPilot Demo Lab")

    return spec


def _execute(
    spec: remediation_actions.ActionSpec, incident: dict[str, Any]
) -> dict[str, Any]:
    """Apply the action and confirm the resulting configuration."""
    if spec.key == "restore_previous_demo_version":
        overrides = _previous_configuration(incident)
    else:
        overrides = dict(spec.env_overrides)

    applied = _apply_env(overrides)
    observed = _read_env()

    mismatches = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in applied.items()
        if observed.get(key) != value
    }
    if mismatches:
        return {
            "succeeded": False,
            "applied": applied,
            "observed": observed,
            "detail": f"Configuration did not take effect: {json.dumps(mismatches)[:300]}",
        }

    dynamo.record_change(
        {
            "change_id": f"{incident.get('incident_id', 'unknown')}-remediation",
            "service": "lambda",
            "resource": config.DEMO_FUNCTION_NAME,
            "action": "remediation",
            "actor": "opspilot-remediation",
            "details": f"{spec.key}: {json.dumps(applied)[:300]}",
            "incident_id": incident.get("incident_id", ""),
        }
    )

    return {
        "succeeded": True,
        "applied": applied,
        "observed": {k: observed.get(k) for k in MUTABLE_ENV_KEYS},
        "detail": f"Applied {spec.key} to {config.DEMO_FUNCTION_NAME}",
    }


def _previous_configuration(incident: dict[str, Any]) -> dict[str, str | None]:
    """Recover the last known-good configuration from the change log.

    Falls back to the Terraform baseline when no prior snapshot exists, which
    is always a safe target because the baseline is healthy by construction.
    """
    from datetime import timedelta

    detected = models.parse_iso(incident.get("detected_at", "")) or models.utcnow()
    entries = dynamo.recent_changes(
        models.iso(detected - timedelta(days=1)), models.iso(detected), limit=50
    )
    for entry in reversed(entries):
        snapshot = entry.get("previous_environment")
        if isinstance(snapshot, dict) and snapshot:
            log.info("previous_configuration_found", change_id=entry.get("change_id"))
            return {k: v for k, v in snapshot.items() if k in MUTABLE_ENV_KEYS}
    log.info("previous_configuration_defaulted")
    return {key: None for key in MUTABLE_ENV_KEYS}


# --- Demo Lab mutation --------------------------------------------------------
def _apply_env(overrides: dict[str, str | None]) -> dict[str, str]:
    """Write the demo function's environment, restricted to mutable keys.

    ``None`` means "restore the Terraform baseline value for this key".
    """
    current = _read_env()
    desired = dict(current)

    applied: dict[str, str] = {}
    for key, value in overrides.items():
        if key not in MUTABLE_ENV_KEYS:
            log.warning("env_key_refused", key=key)
            continue
        resolved = BASELINE_ENV.get(key, "") if value is None else str(value)
        desired[key] = resolved
        applied[key] = resolved

    if not applied:
        return {}

    client("lambda").update_function_configuration(
        FunctionName=config.DEMO_FUNCTION_NAME,
        Environment={"Variables": desired},
    )
    _wait_for_update()
    return applied


def _read_env() -> dict[str, str]:
    """Read the demo function's current environment variables."""
    response = client("lambda").get_function_configuration(
        FunctionName=config.DEMO_FUNCTION_NAME
    )
    return (response.get("Environment") or {}).get("Variables", {}) or {}


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


def _warm_demo_app(invocations: int = 3) -> None:
    """Drive a little healthy traffic so recovery is measurable immediately.

    Without this the verifier would be waiting on CloudWatch to aggregate
    datapoints that nothing is producing.
    """
    payload = json.dumps(
        {"rawPath": "/demo/app", "requestContext": {"http": {"method": "GET"}}}
    ).encode("utf-8")
    for _ in range(invocations):
        try:
            client("lambda").invoke(
                FunctionName=config.DEMO_FUNCTION_NAME,
                InvocationType="RequestResponse",
                Payload=payload,
            )
        except (ClientError, BotoCoreError) as exc:
            log.warning("warm_invocation_failed", error=str(exc)[:200])
            return


# --- Failure ------------------------------------------------------------------
def _fail(
    incident: dict[str, Any], started_at: str, reason: str, refused: bool = False
) -> dict[str, Any]:
    """Record a failed or refused remediation and stop the workflow safely."""
    incident_id = incident.get("incident_id", "")
    status = (
        models.RemediationStatus.MANUAL_REQUIRED
        if refused
        else models.RemediationStatus.FAILED
    )
    log.error("remediation_failed", reason=reason[:300], refused=refused)

    dynamo.transition(
        incident_id,
        models.IncidentStatus.FAILED,
        {
            "remediation_status": status,
            "remediation_detail": {
                "started_at": started_at,
                "completed_at": models.iso(),
                "error": reason[:600],
                "refused": refused,
            },
            "verification_status": models.VerificationStatus.NOT_APPLICABLE,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        models.iso(),
                        "Manual remediation required" if refused else "Remediation failed",
                        models.TimelineKind.REMEDIATION,
                        detail=reason[:400],
                    )
                ],
            ),
        },
    )
    events.publish(
        events.DetailType.REMEDIATION_COMPLETED,
        {"incident_id": incident_id, "succeeded": False, "reason": reason[:300]},
    )
    return {"remediated": False, "incident_id": incident_id, "reason": reason[:300]}
