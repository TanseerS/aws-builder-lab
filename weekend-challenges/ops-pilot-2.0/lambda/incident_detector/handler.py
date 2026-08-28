"""Incident Detector - turns a CloudWatch alarm transition into an incident.

Triggered by ``CloudWatch Alarm State Change`` events on the default event bus.
Its only job is to open exactly one incident per real alarm transition and hand
off to the investigator via EventBridge.
"""

from __future__ import annotations

import json
import os
from typing import Any

from opspilot import dynamo, events, models
from opspilot.logging_utils import get_logger

log = get_logger("incident_detector")

#: Terraform-generated map of alarm name -> incident metadata. Detection is a
#: table lookup, never a guess, so a renamed alarm fails loudly instead of
#: silently mis-classifying an incident.
ALARM_CATALOG: dict[str, dict[str, str]] = json.loads(os.environ.get("ALARM_CATALOG", "{}"))

DEFAULT_SEVERITY = os.environ.get("DEFAULT_SEVERITY", models.Severity.HIGH)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one CloudWatch alarm state-change event."""
    detail = event.get("detail") or {}
    alarm_name = detail.get("alarmName") or event.get("alarm_name", "")
    log.bind(alarm_name=alarm_name, request_id=getattr(context, "aws_request_id", None))

    new_state = (detail.get("state") or {}).get("value", "")
    previous_state = (detail.get("previousState") or {}).get("value", "")

    if new_state != "ALARM":
        log.info("alarm_ignored", new_state=new_state, previous_state=previous_state)
        return {"created": False, "reason": f"state={new_state or 'unknown'}"}

    seed = _build_seed(event, detail, alarm_name)
    dedupe = models.dedupe_key(alarm_name, seed.detected_at)
    incident_id = models.new_incident_id(models.parse_iso(seed.detected_at), seed=dedupe)
    log.bind(incident_id=incident_id)

    item = models.build_incident(seed, incident_id, dedupe)

    if not dynamo.put_incident_if_new(item):
        log.info("incident_deduplicated", dedupe_key=dedupe)
        return {"created": False, "incident_id": incident_id, "reason": "duplicate"}

    log.info(
        "incident_created",
        severity=seed.severity,
        incident_type=seed.incident_type,
        affected_service=seed.affected_service,
    )

    published = events.publish(
        events.DetailType.INCIDENT_DETECTED,
        {
            "incident_id": incident_id,
            "alarm_name": alarm_name,
            "incident_type": seed.incident_type,
            "affected_service": seed.affected_service,
            "severity": seed.severity,
            "detected_at": seed.detected_at,
        },
    )
    if not published:
        # The incident exists and is visible; investigation can be retried from
        # the dashboard rather than losing the incident entirely.
        dynamo.update_incident(
            incident_id,
            {
                "timeline": models.merge_timeline(
                    item["timeline"],
                    [
                        models.timeline_entry(
                            models.iso(),
                            "Investigation dispatch failed - retry from the dashboard",
                            models.TimelineKind.OPSPILOT,
                        )
                    ],
                )
            },
        )

    return {"created": True, "incident_id": incident_id, "dispatched": published}


def _build_seed(
    event: dict[str, Any], detail: dict[str, Any], alarm_name: str
) -> models.IncidentSeed:
    """Derive incident facts from the alarm event and the Terraform catalog."""
    catalog = ALARM_CATALOG.get(alarm_name, {})
    state = detail.get("state") or {}
    reason = str(state.get("reason", ""))[:800]
    detected_at = models.iso(models.parse_iso(state.get("timestamp") or event.get("time")))

    configuration = detail.get("configuration") or {}
    metrics = configuration.get("metrics") or []
    namespace = metric_name = ""
    if metrics:
        stat = (metrics[0].get("metricStat") or {}).get("metric") or {}
        namespace = stat.get("namespace", "")
        metric_name = stat.get("name", "")

    incident_type = catalog.get("incident_type", "unknown")
    service = catalog.get("affected_service", alarm_name or "unknown-service")
    title = catalog.get("title") or f"Alarm {alarm_name} entered ALARM"
    description = catalog.get("description") or configuration.get("description") or reason

    return models.IncidentSeed(
        alarm_name=alarm_name,
        affected_service=service,
        incident_type=incident_type,
        severity=models.Severity.normalise(catalog.get("severity"), DEFAULT_SEVERITY),
        title=title,
        description=description[:1000],
        detected_at=detected_at,
        source="cloudwatch-alarm",
        alarm_reason=reason,
        metric_namespace=namespace or catalog.get("metric_namespace", ""),
        metric_name=metric_name or catalog.get("metric_name", ""),
    )
