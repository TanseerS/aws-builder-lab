"""Verifier - decides whether the service actually recovered.

An incident is never marked resolved because remediation returned without an
exception. This function actively probes the real service over a short window
and only then makes a claim about recovery.

Signals, in order of authority:

1. A live invocation of the demo application (does it work right now?).
2. Error and latency metrics across the verification window.
3. The CloudWatch alarm state, which is corroborating rather than decisive
   because alarm evaluation lags the underlying recovery by design.
"""

from __future__ import annotations

import os
import time
from typing import Any

from opspilot import config, dynamo, events, evidence, models
from opspilot.logging_utils import get_logger

log = get_logger("verifier")

DEMO_LOG_GROUP = os.environ.get("DEMO_LOG_GROUP", "")
METRIC_CATALOG: dict[str, list[dict[str, Any]]] = evidence.load_metric_catalog(
    os.environ.get("METRIC_CATALOG", "{}")
)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run the recovery verification window for one incident."""
    detail = event.get("detail") or {}
    incident_id = detail.get("incident_id", "")
    log.bind(incident_id=incident_id, request_id=getattr(context, "aws_request_id", None))

    if not detail.get("succeeded", True):
        log.info("verification_skipped", reason="remediation did not succeed")
        return {"verified": False, "reason": "remediation failed"}

    incident = dynamo.get_incident(incident_id) if incident_id else None
    if incident is None:
        log.error("incident_not_found")
        return {"verified": False, "reason": "incident not found"}

    if incident.get("status") != models.IncidentStatus.VERIFYING:
        log.warning("verification_skipped", current_status=incident.get("status"))
        return {"verified": False, "reason": "incident not in VERIFYING state"}

    checks = _run_verification_window(incident, context)
    verdict = _decide(checks)

    log.info(
        "verification_decided",
        verified=verdict["verified"],
        checks=len(checks),
        reason=verdict["reason"],
    )
    return _record(incident, checks, verdict)


def _run_verification_window(incident: dict[str, Any], context: Any) -> list[dict[str, Any]]:
    """Probe the service repeatedly across the verification window.

    Sleeping inside the function keeps the design to a single Lambda with no
    orchestration service; the window is bounded well inside the timeout.
    """
    total = max(1, config.VERIFICATION_CHECKS)
    interval = max(1, config.VERIFICATION_INTERVAL_SECONDS)
    alarm_name = incident.get("alarm_name", "")
    checks: list[dict[str, Any]] = []

    for index in range(total):
        if index > 0:
            remaining_ms = _remaining_ms(context)
            # Leave headroom to persist the verdict even if time runs short.
            if remaining_ms is not None and remaining_ms < (interval * 1000) + 20_000:
                log.warning("verification_window_truncated", completed_checks=index)
                break
            time.sleep(interval)

        elapsed = index * interval
        probe = evidence.probe_demo_app(config.DEMO_FUNCTION_NAME)
        alarm_state = evidence.get_alarm_states([alarm_name]).get(alarm_name, "UNKNOWN")

        check = {
            "offset_seconds": elapsed,
            "checked_at": models.iso(),
            "alarm_state": alarm_state,
            "probe_available": probe["available"],
            "healthy": bool(probe["available"] and probe["items"].get("healthy")),
            "status_code": probe["items"].get("status_code") if probe["available"] else None,
            "duration_ms": probe["items"].get("duration_ms") if probe["available"] else None,
            "function_error": probe["items"].get("function_error", "") if probe["available"] else "",
            "note": "" if probe["available"] else probe["error"],
        }
        checks.append(check)
        log.info(
            "verification_check",
            offset_seconds=elapsed,
            healthy=check["healthy"],
            alarm_state=alarm_state,
            duration_ms=check["duration_ms"],
        )

    checks.append(_metric_check(incident))
    return checks


def _metric_check(incident: dict[str, Any]) -> dict[str, Any]:
    """Summarise error metrics over the verification window."""
    from datetime import timedelta

    end = models.utcnow()
    start = end - timedelta(
        seconds=max(180, config.VERIFICATION_CHECKS * config.VERIFICATION_INTERVAL_SECONDS)
    )
    probes = METRIC_CATALOG.get(incident.get("incident_type", ""), []) or METRIC_CATALOG.get(
        "default", []
    )

    error_total = 0.0
    collected = 0
    details: list[dict[str, Any]] = []
    for probe in probes:
        if not probe.get("error_signal"):
            continue
        series = evidence.get_metric_series(
            namespace=probe.get("namespace", ""),
            metric_name=probe.get("metric_name", ""),
            dimensions=probe.get("dimensions", {}),
            start=start,
            end=end,
            statistic=probe.get("statistic", "Sum"),
            period_seconds=60,
        )
        if not series["available"]:
            continue
        collected += 1
        summary = evidence.summarise_series(series["items"])
        error_total += float(summary["sum"])
        details.append({"metric": probe.get("metric_name"), **summary})

    return {
        "offset_seconds": -1,
        "kind": "metrics",
        "checked_at": models.iso(),
        "metrics_available": collected > 0,
        "error_metric_sum": round(error_total, 3),
        "metrics": details,
        "note": "Error metric evidence unavailable" if collected == 0 else "",
    }


def _decide(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn the collected checks into a recovery verdict.

    The live probe is decisive. The alarm is reported but not allowed to veto a
    healthy service, because CloudWatch evaluation lag routinely leaves an alarm
    in ALARM for a minute or two after real recovery - that lag is stated
    explicitly in the verdict rather than glossed over.
    """
    probes = [c for c in checks if c.get("kind") != "metrics"]
    metrics = next((c for c in checks if c.get("kind") == "metrics"), {})

    if not probes:
        return {"verified": False, "reason": "No verification probes completed"}

    usable = [c for c in probes if c.get("probe_available")]
    if not usable:
        return {"verified": False, "reason": "Application health probe was unavailable"}

    final = usable[-1]
    if not final["healthy"]:
        return {
            "verified": False,
            "reason": f"Application still unhealthy at the end of the window "
                      f"(status {final.get('status_code')}, "
                      f"error {final.get('function_error') or 'none'})",
        }

    healthy_count = sum(1 for c in usable if c["healthy"])
    if healthy_count < max(1, len(usable) - 1):
        return {
            "verified": False,
            "reason": f"Application recovered intermittently "
                      f"({healthy_count}/{len(usable)} probes healthy)",
        }

    error_sum = metrics.get("error_metric_sum", 0)
    if metrics.get("metrics_available") and error_sum > 0:
        # Errors from before the fix still sit inside the metric window, so this
        # is only decisive when the live service is also unhealthy - which it
        # is not at this point. Report it as a caveat.
        caveat = f"error metrics still show {error_sum} event(s) in the window (pre-fix datapoints)"
    else:
        caveat = ""

    alarm_state = final.get("alarm_state", "UNKNOWN")
    if alarm_state == "ALARM":
        reason = (
            f"Application healthy on {healthy_count}/{len(usable)} probes; alarm still "
            "clearing (CloudWatch evaluation lag)"
        )
    else:
        reason = (
            f"Application healthy on {healthy_count}/{len(usable)} probes; "
            f"alarm state {alarm_state}"
        )
    if caveat:
        reason = f"{reason}; {caveat}"

    return {"verified": True, "reason": reason}


def _record(
    incident: dict[str, Any], checks: list[dict[str, Any]], verdict: dict[str, Any]
) -> dict[str, Any]:
    """Persist the verdict and drive the incident to its terminal state."""
    incident_id = incident.get("incident_id", "")
    verified = verdict["verified"]
    now = models.iso()

    verification_detail = {
        "status": (
            models.VerificationStatus.VERIFIED
            if verified
            else models.VerificationStatus.VERIFICATION_FAILED
        ),
        "reason": verdict["reason"],
        "checks": checks,
        "window_seconds": config.VERIFICATION_CHECKS * config.VERIFICATION_INTERVAL_SECONDS,
        "completed_at": now,
    }

    updates: dict[str, Any] = {
        "verification_status": verification_detail["status"],
        "verification_detail": verification_detail,
        "timeline": models.merge_timeline(
            incident.get("timeline"),
            [
                models.timeline_entry(
                    now,
                    "Recovery verified" if verified else "Recovery verification failed",
                    models.TimelineKind.VERIFICATION,
                    detail=verdict["reason"],
                )
            ],
        ),
    }

    if verified:
        updates["resolved_at"] = now
        updates["time_to_resolve_minutes"] = models.minutes_between(
            incident.get("detected_at"), now
        )
        target = models.IncidentStatus.RESOLVED
    else:
        target = models.IncidentStatus.FAILED

    dynamo.transition(incident_id, target, updates)

    events.publish(
        events.DetailType.VERIFICATION_COMPLETED,
        {
            "incident_id": incident_id,
            "verified": verified,
            "status": target,
            "reason": verdict["reason"][:300],
        },
    )
    return {"verified": verified, "incident_id": incident_id, "status": target}


def _remaining_ms(context: Any) -> int | None:
    """Milliseconds left in this invocation, if the runtime exposes it."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:  # noqa: BLE001 - context shape varies in tests
            return None
    return None
