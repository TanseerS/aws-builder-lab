"""Investigator - collects evidence, correlates changes, asks Bedrock why.

The division of labour is deliberate and absolute:

* Deterministic Lambda code queries AWS and normalises the facts.
* Bedrock receives only that normalised evidence and returns an explanation.
* Bedrock is given no tools, no credentials and no ability to act.

Every evidence source degrades independently: if CloudTrail, logs, metrics or
Bedrock itself fail, the incident still exists and says honestly what is
missing.
"""

from __future__ import annotations

import os
from typing import Any

from opspilot import (
    bedrock,
    change_correlator,
    config,
    dynamo,
    evidence,
    events,
    models,
    prompts,
    remediation_actions,
)
from opspilot.logging_utils import get_logger

log = get_logger("investigator")

DEMO_LOG_GROUP = os.environ.get("DEMO_LOG_GROUP", "")
#: Terraform-supplied metric probes per incident type, rehydrated from the
#: compact form that fits inside Lambda's 4 KB environment limit.
METRIC_CATALOG: dict[str, list[dict[str, Any]]] = evidence.load_metric_catalog(
    os.environ.get("METRIC_CATALOG", "{}")
)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Investigate one incident end to end."""
    detail = event.get("detail") or {}
    incident_id = detail.get("incident_id") or event.get("incident_id", "")
    log.bind(incident_id=incident_id, request_id=getattr(context, "aws_request_id", None))

    if not incident_id:
        log.error("missing_incident_id")
        return {"investigated": False, "reason": "missing incident_id"}

    incident = dynamo.get_incident(incident_id)
    if incident is None:
        log.error("incident_not_found")
        return {"investigated": False, "reason": "incident not found"}

    started = dynamo.transition(
        incident_id,
        models.IncidentStatus.INVESTIGATING,
        {
            "investigation_count": int(incident.get("investigation_count", 0)) + 1,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        models.iso(),
                        "OpsPilot investigation started",
                        models.TimelineKind.OPSPILOT,
                    )
                ],
            ),
        },
        expected_status=(
            models.IncidentStatus.DETECTED,
            models.IncidentStatus.INVESTIGATING,
            models.IncidentStatus.ROOT_CAUSE_IDENTIFIED,
            models.IncidentStatus.AWAITING_APPROVAL,
            models.IncidentStatus.VERIFYING,
            models.IncidentStatus.RESOLVED,
            models.IncidentStatus.FAILED,
        ),
    )
    if started is None:
        log.warning("investigation_skipped", current_status=incident.get("status"))
        return {"investigated": False, "reason": "incident not in an investigable state"}

    incident = started
    bundle = _collect_evidence(incident)
    analysis, ai_status = _analyse(incident, bundle)
    updates = _build_updates(incident, bundle, analysis, ai_status)

    target_status = (
        models.IncidentStatus.AWAITING_APPROVAL
        if updates["remediation_status"] == models.RemediationStatus.AWAITING_APPROVAL
        else models.IncidentStatus.ROOT_CAUSE_IDENTIFIED
    )
    dynamo.transition(incident_id, target_status, updates)

    events.publish(
        events.DetailType.INVESTIGATION_COMPLETED,
        {
            "incident_id": incident_id,
            "status": target_status,
            "ai_status": ai_status,
            "confidence": updates.get("confidence", 0),
            "awaiting_approval": target_status == models.IncidentStatus.AWAITING_APPROVAL,
        },
    )

    log.info(
        "investigation_completed",
        status=target_status,
        ai_status=ai_status,
        confidence=updates.get("confidence"),
        changes=len(updates.get("changes", [])),
    )
    return {
        "investigated": True,
        "incident_id": incident_id,
        "status": target_status,
        "ai_status": ai_status,
    }


# --- Evidence -----------------------------------------------------------------
def _collect_evidence(incident: dict[str, Any]) -> dict[str, Any]:
    """Gather every evidence source, tolerating individual failures."""
    start, end = evidence.incident_window(incident.get("detected_at", ""))
    sources: dict[str, Any] = {}

    alarm = evidence.get_alarm(incident.get("alarm_name", ""))
    sources["cloudwatch_alarm"] = _source_note(alarm, "Alarm evidence unavailable")

    metrics = _collect_metrics(incident, start, end, sources)

    logs = evidence.get_recent_logs(DEMO_LOG_GROUP, start, end)
    sources["cloudwatch_logs"] = _source_note(logs, "Log evidence unavailable")

    app_state = evidence.get_function_state(config.DEMO_FUNCTION_NAME)
    sources["application_state"] = _source_note(app_state, "Application state unavailable")

    correlation = change_correlator.collect_changes(incident)
    sources.update(correlation["sources"])

    try:
        similar = dynamo.find_similar_incidents(incident, config.MAX_SIMILAR_INCIDENTS)
        sources["incident_memory"] = {
            "available": True,
            "count": len(similar),
            "note": "Deterministic recall on (affected_service, incident_type)",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("similar_lookup_failed", error=str(exc)[:200])
        similar, sources["incident_memory"] = [], {
            "available": False, "count": 0, "note": "Incident memory unavailable"
        }

    return {
        "alarm": alarm["items"] if alarm["available"] else None,
        "metrics": metrics,
        "logs": logs["items"] if logs["available"] else None,
        "application_state": app_state["items"] if app_state["available"] else None,
        "changes": correlation["changes"],
        "change_window": correlation["window"],
        "similar_incidents": similar,
        "sources": sources,
        "window": {"start": models.iso(start), "end": models.iso(end)},
    }


def _collect_metrics(
    incident: dict[str, Any],
    start: Any,
    end: Any,
    sources: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch the metric probes configured for this incident type."""
    probes = METRIC_CATALOG.get(incident.get("incident_type", ""), [])
    if not probes:
        probes = METRIC_CATALOG.get("default", [])

    collected: list[dict[str, Any]] = []
    failures = 0
    for probe in probes:
        series = evidence.get_metric_series(
            namespace=probe.get("namespace", ""),
            metric_name=probe.get("metric_name", ""),
            dimensions=probe.get("dimensions", {}),
            start=start,
            end=end,
            statistic=probe.get("statistic", "Sum"),
            period_seconds=int(probe.get("period", 60)),
        )
        if not series["available"]:
            failures += 1
            continue
        collected.append(
            {
                "metric": probe.get("metric_name", ""),
                "namespace": probe.get("namespace", ""),
                "statistic": probe.get("statistic", "Sum"),
                "label": probe.get("label", probe.get("metric_name", "")),
                "summary": evidence.summarise_series(series["items"]),
                "points": series["items"][-20:],
            }
        )

    sources["cloudwatch_metrics"] = {
        "available": bool(collected),
        "count": len(collected),
        "note": (
            "Metric evidence unavailable"
            if not collected
            else f"{len(collected)} metric series collected"
            + (f", {failures} probe(s) failed" if failures else "")
        ),
    }
    return collected


def _source_note(result: evidence.EvidenceResult, fallback: str) -> dict[str, Any]:
    """Render an evidence source's availability for storage and the prompt."""
    if result["available"]:
        note = "collected"
    else:
        note = result["error"] or fallback
    return {
        "available": result["available"],
        "note": note,
        # Carry through collector metadata (log_group, total_seen, truncated...).
        **{k: v for k, v in result.items() if k not in {"available", "items", "error"}},
    }


# --- Analysis -----------------------------------------------------------------
def _analyse(incident: dict[str, Any], bundle: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Ask Bedrock for a root cause, degrading honestly if it cannot answer."""
    prompt = prompts.build_root_cause_prompt(incident, bundle)
    log.info("bedrock_request_prepared", prompt_chars=len(prompt),
             model_id=config.BEDROCK_MODEL_ID)

    try:
        raw = bedrock.converse(prompt, system_prompt=prompts.ROOT_CAUSE_SYSTEM_PROMPT)
    except bedrock.BedrockUnavailable as exc:
        log.error("bedrock_unavailable", error=str(exc)[:300])
        return prompts.fallback_analysis(f"Bedrock unavailable: {exc}"), models.AIStatus.UNAVAILABLE

    parsed = bedrock.parse_json_response(raw)
    if parsed is None:
        log.warning("bedrock_response_unparseable")
        return (
            prompts.fallback_analysis("Model returned output that could not be parsed as JSON"),
            models.AIStatus.FALLBACK,
        )

    validated = prompts.validate_analysis(parsed)
    if validated is None:
        log.warning("bedrock_response_invalid")
        return (
            prompts.fallback_analysis("Model response did not match the required schema"),
            models.AIStatus.FALLBACK,
        )

    return validated, models.AIStatus.OK


def _build_updates(
    incident: dict[str, Any],
    bundle: dict[str, Any],
    analysis: dict[str, Any],
    ai_status: str,
) -> dict[str, Any]:
    """Assemble the incident update produced by this investigation."""
    incident_type = incident.get("incident_type", "unknown")
    changes = bundle["changes"]

    recommendations = remediation_actions.annotate_recommendations(
        analysis.get("recommended_actions"), incident_type
    )
    executable = [r for r in recommendations if r["executable"]]

    # With no usable model recommendation, OpsPilot still offers the safe
    # scenario-appropriate default rather than stalling the incident.
    if not executable:
        spec = remediation_actions.default_action_for(incident_type)
        recommendations.append(
            {
                "proposed_action": spec.key,
                "action": spec.key,
                "title": spec.title,
                "risk": spec.risk,
                "reason": (
                    "Safe default for this failure scenario"
                    if ai_status == models.AIStatus.OK
                    else "AI analysis unavailable; safe default for this failure scenario"
                ),
                "allowlisted": True,
                "applicable": True,
                "executable": True,
                "source": "opspilot-default",
            }
        )
        executable = [recommendations[-1]]

    timeline = models.merge_timeline(
        incident.get("timeline"),
        [
            *_change_timeline(changes),
            *_model_timeline(analysis.get("timeline")),
            models.timeline_entry(
                models.iso(),
                (
                    "Root cause identified"
                    if ai_status == models.AIStatus.OK
                    else "Investigation completed without AI analysis"
                ),
                models.TimelineKind.OPSPILOT,
                detail=analysis.get("summary", ""),
            ),
        ],
    )

    root_cause = analysis["root_cause"]
    confidence = root_cause.get("confidence", 0)
    severity = (
        analysis["severity"]
        if ai_status == models.AIStatus.OK
        else incident.get("severity", models.Severity.HIGH)
    )

    evidence_list = list(analysis.get("evidence", []))
    change_summary = change_correlator.summarise_changes(changes)
    if change_summary and change_summary not in evidence_list:
        evidence_list.insert(0, f"Change correlation: {change_summary}")

    return {
        "severity": severity,
        "ai_status": ai_status,
        "ai_summary": analysis.get("summary", ""),
        "root_cause": {
            "description": root_cause.get("description", ""),
            "confidence": confidence,
            "category": root_cause.get("category", "unknown"),
        },
        "confidence": confidence,
        "evidence": evidence_list,
        "contributing_factors": analysis.get("contributing_factors", []),
        "changes": changes[:40],
        "change_summary": change_summary,
        "change_window": bundle["change_window"],
        "timeline": timeline,
        "recommendations": recommendations,
        "similar_incidents": bundle["similar_incidents"],
        "evidence_sources": bundle["sources"],
        "metrics_snapshot": bundle["metrics"],
        "remediation_status": models.RemediationStatus.AWAITING_APPROVAL,
        "fallback_reason": analysis.get("_fallback_reason", ""),
    }


def _change_timeline(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Promote correlated changes onto the incident timeline."""
    entries: list[dict[str, Any]] = []
    for change in changes[:10]:
        if change.get("correlation") == "unrelated":
            continue
        target = change.get("resource") or change.get("service", "resource")
        entries.append(
            models.timeline_entry(
                change.get("timestamp", ""),
                f"{change.get('action', 'Change')} on {target}",
                models.TimelineKind.CHANGE,
                source=change.get("source", "cloudtrail"),
                detail=f"correlation: {change.get('correlation')} "
                       f"(score {change.get('correlation_score')})",
            )
        )
    return entries


def _model_timeline(model_timeline: Any) -> list[dict[str, Any]]:
    """Include model-proposed timeline entries, clearly attributed."""
    entries: list[dict[str, Any]] = []
    for item in (model_timeline or [])[:10]:
        stamp = item.get("timestamp")
        if not stamp:
            continue
        entries.append(
            models.timeline_entry(
                stamp,
                item.get("event", ""),
                models.TimelineKind.METRIC,
                source="bedrock-analysis",
            )
        )
    return entries
