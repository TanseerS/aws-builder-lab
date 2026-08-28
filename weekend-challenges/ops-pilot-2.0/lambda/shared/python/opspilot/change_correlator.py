"""Change correlation: what changed immediately before this incident?

Two independent sources are merged:

``opspilot-change-log``
    Written synchronously the moment OpsPilot itself changes the demo
    environment. Sub-second latency, but only covers OpsPilot-driven changes.

``cloudtrail``
    The authoritative record of AWS control-plane activity, including changes
    OpsPilot did not make. Delivery is *not* instantaneous, so a very recent
    change may not appear yet.

Correlation is deterministic scoring, not AI. Bedrock later receives the ranked
result and is told plainly which entries merely occurred and which plausibly
contributed.

Implementation note: this lives in the shared layer rather than as its own
Lambda because it is pure computation over already-collected evidence; a
separate function would add a network hop and an IAM role for no benefit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from . import config, dynamo, evidence, models
from .logging_utils import get_logger

log = get_logger("change_correlator")

#: Weight given to a change based on how close it sits to incident onset.
_PROXIMITY_BANDS: Final[tuple[tuple[float, float, str], ...]] = (
    (2.0, 0.45, "within 2 minutes of incident onset"),
    (5.0, 0.35, "within 5 minutes of incident onset"),
    (15.0, 0.20, "within 15 minutes of incident onset"),
    (60.0, 0.05, "within the hour before incident onset"),
)

#: Actions whose blast radius makes them likely incident triggers.
_HIGH_IMPACT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "UpdateFunctionConfiguration", "UpdateFunctionCode", "PublishVersion",
        "UpdateAlias", "DeleteFunction", "PutFunctionConcurrency",
        "UpdateTable", "DeleteTable", "UpdateStack", "ExecuteChangeSet",
        "PutRolePolicy", "DetachRolePolicy", "DeleteRolePolicy",
        "UpdateStage", "CreateDeployment", "UpdateIntegration",
        "fault_injection", "configuration_change",
    }
)

#: Changes that move a system *towards* its known-good state. A restore, reset
#: or remediation cannot plausibly be the cause of a new failure, so these are
#: capped below the contributor threshold no matter how recent they are.
#: Without this, OpsPilot's own reset would outrank the fault that broke things.
_RESTORATIVE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"remediation", "configuration_reset", "restore", "rollback"}
)

#: Service keywords linking a change to the failing service.
_SERVICE_HINTS: Final[dict[str, tuple[str, ...]]] = {
    "lambda_error": ("lambda",),
    "lambda_latency": ("lambda",),
    "application_error": ("lambda", "apigateway"),
    "database_throttle": ("dynamodb", "lambda"),
    "configuration_error": ("lambda", "ssm", "secretsmanager"),
}


def collect_changes(incident: dict[str, Any]) -> dict[str, Any]:
    """Gather, normalise, correlate and rank changes around an incident.

    Returns a dict carrying the ranked changes plus per-source availability so
    the UI can distinguish "no changes found" from "CloudTrail unavailable".
    """
    detected_at = incident.get("detected_at") or models.iso()
    detected = models.parse_iso(detected_at) or models.utcnow()
    lookback = config.CHANGE_LOOKBACK_MINUTES
    start = detected - timedelta(minutes=lookback)
    # A small forward window catches remediation and late-arriving records.
    end = min(detected + timedelta(minutes=5), models.utcnow())
    if end <= start:
        end = start + timedelta(minutes=1)

    sources: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []

    # 1. OpsPilot's own change log - immediate, high fidelity.
    try:
        logged = dynamo.recent_changes(models.iso(start), models.iso(end), limit=100)
        changes.extend(_normalise_change_log(entry) for entry in logged)
        sources["opspilot_change_log"] = {
            "available": True,
            "count": len(logged),
            "note": "Changes recorded by OpsPilot at the moment they were applied",
        }
    except Exception as exc:  # noqa: BLE001 - one source must not break the rest
        log.warning("change_log_unavailable", error=str(exc)[:200])
        sources["opspilot_change_log"] = {
            "available": False,
            "count": 0,
            "note": "OpsPilot change log unavailable",
        }

    # 2. CloudTrail - authoritative, but eventually delivered.
    trail = evidence.get_cloudtrail_changes(start, end)
    if trail["available"]:
        seen_keys = {(c["action"], c["resource"], c["timestamp"][:16]) for c in changes}
        for entry in trail["items"]:
            key = (entry["action"], entry["resource"], entry["timestamp"][:16])
            if key not in seen_keys:
                changes.append(entry)
                seen_keys.add(key)
        sources["cloudtrail"] = {
            "available": True,
            "count": trail.get("returned", len(trail["items"])),
            "truncated": trail.get("truncated", False),
            "note": (
                "CloudTrail delivery is not instantaneous; very recent changes "
                "may not appear yet"
            ),
        }
    else:
        sources["cloudtrail"] = {
            "available": False,
            "count": 0,
            "note": f"CloudTrail evidence unavailable: {trail['error']}",
        }

    ranked = rank_changes(changes, detected, incident.get("incident_type", "unknown"))
    log.info(
        "changes_collected",
        incident_id=incident.get("incident_id"),
        total=len(ranked),
        contributing=sum(1 for c in ranked if c["correlation"] == "likely_contributor"),
        lookback_minutes=lookback,
    )
    return {
        "changes": ranked,
        "sources": sources,
        "window": {"start": models.iso(start), "end": models.iso(end),
                   "lookback_minutes": lookback},
    }


def _normalise_change_log(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpsPilot change-log row into the common change shape."""
    return {
        "timestamp": entry.get("timestamp", ""),
        "service": entry.get("service", "opspilot"),
        "resource": entry.get("resource", ""),
        "action": entry.get("action", "configuration_change"),
        "actor": entry.get("actor", "opspilot"),
        "details": str(entry.get("details", ""))[:400],
        "source": "opspilot-change-log",
        "event_id": entry.get("change_id", ""),
    }


def rank_changes(
    changes: list[dict[str, Any]],
    detected: datetime,
    incident_type: str = "unknown",
) -> list[dict[str, Any]]:
    """Score each change and label it as contributor, candidate or unrelated.

    The score blends three deterministic signals: temporal proximity to onset,
    the blast radius of the operation, and whether the changed service matches
    the failing one. It never asserts causation - the label is explicitly a
    correlation strength.
    """
    hints = _SERVICE_HINTS.get(incident_type, ())
    scored: list[dict[str, Any]] = []

    for change in changes:
        moment = models.parse_iso(change.get("timestamp"))
        score = 0.0
        reasons: list[str] = []

        # Temporal proximity - changes after onset cannot have caused it.
        if moment is not None:
            delta_minutes = (detected - moment).total_seconds() / 60.0
            change["minutes_before_incident"] = round(delta_minutes, 2)
            if delta_minutes < -0.5:
                change["correlation"] = "after_incident"
                change["correlation_score"] = 0.0
                change["correlation_reasons"] = ["Occurred after the incident began"]
                scored.append(change)
                continue
            for limit, weight, label in _PROXIMITY_BANDS:
                if delta_minutes <= limit:
                    score += weight
                    reasons.append(label)
                    break
        else:
            change["minutes_before_incident"] = None

        # Blast radius of the operation itself.
        action = change.get("action", "")
        if action in _HIGH_IMPACT_ACTIONS:
            score += 0.30
            reasons.append(f"{action} can change runtime behaviour")

        # Does the change touch the service that is failing?
        service = str(change.get("service", "")).lower()
        if hints and any(hint in service for hint in hints):
            score += 0.20
            reasons.append(f"Affects {service}, the service reporting the failure")

        # OpsPilot-recorded changes are exact, not inferred.
        if change.get("source") == "opspilot-change-log":
            score += 0.10
            reasons.append("Recorded directly by OpsPilot at the moment of change")

        # Restorative changes are capped: they cannot have caused the failure.
        if action in _RESTORATIVE_ACTIONS:
            score = min(score, 0.30)
            reasons.append(
                "Restorative change: moves the system towards its healthy baseline"
            )

        score = round(min(score, 1.0), 2)
        change["correlation_score"] = score
        change["correlation_reasons"] = reasons
        change["correlation"] = (
            "likely_contributor" if score >= 0.6
            else "possible_contributor" if score >= 0.35
            else "unrelated"
        )
        scored.append(change)

    scored.sort(
        key=lambda c: (-float(c.get("correlation_score", 0)), str(c.get("timestamp", "")))
    )
    return scored


def primary_change(changes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single most likely contributing change, if any."""
    for change in changes:
        if change.get("correlation") == "likely_contributor":
            return change
    return None


def summarise_changes(changes: list[dict[str, Any]]) -> str:
    """One-line human summary used in postmortems and the incident list."""
    contributors = [c for c in changes if c.get("correlation") == "likely_contributor"]
    if not contributors:
        possible = [c for c in changes if c.get("correlation") == "possible_contributor"]
        if possible:
            return f"{len(possible)} possible contributing change(s) identified"
        return "No contributing infrastructure changes identified"
    head = contributors[0]
    when = head.get("minutes_before_incident")
    timing = f"{when} minutes before onset" if when is not None else "shortly before onset"
    return f"{head.get('action')} on {head.get('resource') or head.get('service')} {timing}"
