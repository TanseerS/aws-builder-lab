"""Postmortem generator - writes the incident record up as a document.

Every fact in the output (timestamps, metrics, changes, verification results)
is read from the stored incident. Bedrock is asked only for narrative prose,
and if it is unavailable the document is still generated from the data with
deterministic wording. The model is never the source of truth.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from opspilot import bedrock, config, dynamo, events, models, prompts
from opspilot.aws_clients import client
from opspilot.logging_utils import get_logger

log = get_logger("postmortem")

POSTMORTEM_PREFIX = "postmortems"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Generate and store the postmortem for a completed incident."""
    detail = event.get("detail") or {}
    incident_id = detail.get("incident_id", "")
    log.bind(incident_id=incident_id, request_id=getattr(context, "aws_request_id", None))

    incident = dynamo.get_incident(incident_id) if incident_id else None
    if incident is None:
        log.error("incident_not_found")
        return {"generated": False, "reason": "incident not found"}

    if incident.get("status") not in models.IncidentStatus.TERMINAL:
        log.info("postmortem_skipped", status=incident.get("status"))
        return {"generated": False, "reason": "incident not in a terminal state"}

    narrative, narrative_source = _narrative(incident)
    document = _render(incident, narrative, narrative_source)
    key = f"{POSTMORTEM_PREFIX}/{incident_id}.md"

    try:
        client("s3").put_object(
            Bucket=config.ARTIFACTS_BUCKET,
            Key=key,
            Body=document.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            ServerSideEncryption="AES256",
            Metadata={
                "incident-id": incident_id,
                "severity": str(incident.get("severity", "")),
                "narrative-source": narrative_source,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        log.error("postmortem_upload_failed", error=str(exc)[:300])
        dynamo.update_incident(
            incident_id, {"postmortem_location": "", "postmortem_error": str(exc)[:300]}
        )
        return {"generated": False, "reason": f"S3 upload failed: {type(exc).__name__}"}

    location = f"s3://{config.ARTIFACTS_BUCKET}/{key}"
    dynamo.update_incident(
        incident_id,
        {
            "postmortem_location": location,
            "postmortem_key": key,
            "postmortem_generated_at": models.iso(),
            "postmortem_narrative_source": narrative_source,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        models.iso(),
                        "Postmortem generated",
                        models.TimelineKind.OPSPILOT,
                        detail=location,
                    )
                ],
            ),
        },
    )

    events.publish(
        events.DetailType.POSTMORTEM_GENERATED,
        {"incident_id": incident_id, "location": location},
    )
    log.info("postmortem_generated", location=location, narrative_source=narrative_source)
    return {"generated": True, "incident_id": incident_id, "location": location}


# --- Narrative ----------------------------------------------------------------
def _narrative(incident: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Ask Bedrock for prose, falling back to deterministic text."""
    facts = _facts_for_prompt(incident)
    try:
        raw = bedrock.converse(
            prompts.build_postmortem_prompt(facts),
            system_prompt=prompts.POSTMORTEM_SYSTEM_PROMPT,
            max_tokens=1500,
        )
    except bedrock.BedrockUnavailable as exc:
        log.warning("postmortem_narrative_unavailable", error=str(exc)[:200])
        return _deterministic_narrative(incident), "deterministic"

    validated = prompts.validate_postmortem(bedrock.parse_json_response(raw))
    if validated is None:
        log.warning("postmortem_narrative_invalid")
        return _deterministic_narrative(incident), "deterministic"
    return validated, "bedrock"


def _facts_for_prompt(incident: dict[str, Any]) -> dict[str, Any]:
    """The bounded fact sheet handed to the model for narrative writing."""
    root_cause = incident.get("root_cause") or {}
    verification = incident.get("verification_detail") or {}
    return {
        "incident_id": incident.get("incident_id"),
        "title": incident.get("title"),
        "severity": incident.get("severity"),
        "status": incident.get("status"),
        "detected_at": incident.get("detected_at"),
        "resolved_at": incident.get("resolved_at"),
        "time_to_resolve_minutes": incident.get("time_to_resolve_minutes"),
        "affected_service": incident.get("affected_service"),
        "incident_type": incident.get("incident_type"),
        "alarm_name": incident.get("alarm_name"),
        "root_cause": root_cause.get("description"),
        "root_cause_category": root_cause.get("category"),
        "confidence": root_cause.get("confidence"),
        "evidence": (incident.get("evidence") or [])[:10],
        "contributing_factors": (incident.get("contributing_factors") or [])[:6],
        "change_summary": incident.get("change_summary"),
        "contributing_changes": [
            {k: c.get(k) for k in ("timestamp", "action", "resource", "actor", "correlation")}
            for c in (incident.get("changes") or [])[:5]
        ],
        "remediation": (incident.get("remediation_detail") or {}).get("title"),
        "verification": verification.get("reason"),
        "verification_status": incident.get("verification_status"),
        "ai_status": incident.get("ai_status"),
    }


def _deterministic_narrative(incident: dict[str, Any]) -> dict[str, Any]:
    """Narrative sections written from the incident data alone."""
    root_cause = (incident.get("root_cause") or {}).get("description") or "not determined"
    service = incident.get("affected_service", "the affected service")
    mttr = incident.get("time_to_resolve_minutes")
    duration = f"{mttr} minutes" if mttr is not None else "an unrecorded duration"
    remediation = (incident.get("remediation_detail") or {}).get("title") or "no automated action"
    resolved = incident.get("status") == models.IncidentStatus.RESOLVED

    went_well: list[str] = [
        f"The condition was detected automatically by CloudWatch alarm "
        f"{incident.get('alarm_name', 'unknown')}.",
        "Evidence collection, change correlation and analysis ran without manual effort.",
    ]
    if resolved:
        went_well.append("Recovery was verified by probing the live service, not assumed.")

    went_wrong: list[str] = [
        f"{service} was degraded for {duration} before recovery was confirmed."
    ]
    if incident.get("ai_status") != models.AIStatus.OK:
        went_wrong.append(
            "Automated AI analysis was unavailable, so the diagnosis relied on "
            "deterministic evidence only."
        )
    for factor in (incident.get("contributing_factors") or [])[:3]:
        went_wrong.append(str(factor))

    return {
        "executive_summary": (
            f"{incident.get('title', 'An incident')} affected {service}. "
            f"OpsPilot identified the root cause as: {root_cause}. "
            f"The incident was remediated with {remediation} and "
            f"{'recovery was verified' if resolved else 'recovery could not be verified'}."
        ),
        "impact": (
            f"{service} returned errors or degraded responses from "
            f"{incident.get('detected_at', 'detection')} until "
            f"{incident.get('resolved_at') or 'the end of the incident'}."
        ),
        "what_went_well": went_well,
        "what_went_wrong": went_wrong,
        "preventive_actions": [
            "Add a deployment validation alarm so configuration changes are "
            "checked before they reach steady state.",
            "Keep the change log and CloudTrail correlation window aligned with "
            "the deployment cadence.",
        ],
        "lessons_learned": [
            "Correlating control-plane changes with failure onset shortened "
            "diagnosis substantially.",
            "Verifying recovery against the live service prevents premature "
            "incident closure.",
        ],
    }


# --- Rendering ----------------------------------------------------------------
def _render(
    incident: dict[str, Any],
    narrative: dict[str, Any],
    narrative_source: str,
) -> str:
    """Render the Markdown postmortem from incident data plus narrative.

    ``narrative_source`` is passed in rather than read from the incident: it is
    only persisted after this document is uploaded, so reading it back here
    would always report it as unknown.
    """
    incident_id = incident.get("incident_id", "UNKNOWN")
    root_cause = incident.get("root_cause") or {}
    confidence = root_cause.get("confidence", 0)
    verification = incident.get("verification_detail") or {}
    remediation = incident.get("remediation_detail") or {}
    mttr = incident.get("time_to_resolve_minutes")

    lines: list[str] = [
        f"# Incident {incident_id}",
        "",
        f"**{incident.get('title', 'Untitled incident')}**",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Status | {incident.get('status', '')} |",
        f"| Severity | {incident.get('severity', '')} |",
        f"| Affected service | {incident.get('affected_service', '')} |",
        f"| Detected | {incident.get('detected_at', '')} |",
        f"| Resolved | {incident.get('resolved_at') or 'not resolved'} |",
        f"| Time to resolve | {f'{mttr} minutes' if mttr is not None else 'n/a'} |",
        f"| Detection source | {incident.get('source', '')} ({incident.get('alarm_name', '')}) |",
        f"| Analysis | {_ai_label(incident.get('ai_status', ''))} |",
        "",
        "## Executive Summary",
        "",
        narrative.get("executive_summary", ""),
        "",
        "## Impact",
        "",
        narrative.get("impact", ""),
        "",
        "## Detection",
        "",
        f"CloudWatch alarm `{incident.get('alarm_name', 'unknown')}` entered ALARM at "
        f"{incident.get('detected_at', 'an unrecorded time')}, which EventBridge "
        f"delivered to OpsPilot.",
        "",
        f"> {incident.get('alarm_reason', 'No alarm reason recorded.')}",
        "",
        "## Timeline",
        "",
    ]

    timeline = incident.get("timeline") or []
    if timeline:
        lines.append("| Time | Event | Source |")
        lines.append("| --- | --- | --- |")
        for entry in timeline:
            event_text = str(entry.get("event", "")).replace("|", "\\|")
            lines.append(
                f"| {entry.get('timestamp', '')} | {entry.get('icon', '')} {event_text} "
                f"| {entry.get('source', '')} |"
            )
    else:
        lines.append("_No timeline entries were recorded._")

    lines += [
        "",
        "## Root Cause",
        "",
        root_cause.get("description") or "_Root cause was not determined._",
        "",
        f"- **Category:** {root_cause.get('category', 'unknown')}",
        f"- **Confidence:** {_percent(confidence)}",
        f"- **Change correlation:** {incident.get('change_summary', 'none recorded')}",
        "",
        "## Evidence",
        "",
    ]
    lines += _bullets(incident.get("evidence"), "_No evidence was recorded._")

    changes = incident.get("changes") or []
    lines += ["", "### Infrastructure changes examined", ""]
    if changes:
        lines.append("| Time | Action | Resource | Actor | Correlation | Source |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for change in changes[:15]:
            lines.append(
                f"| {change.get('timestamp', '')} | {change.get('action', '')} "
                f"| {str(change.get('resource', ''))[:60]} | {change.get('actor', '')} "
                f"| {change.get('correlation', '')} ({change.get('correlation_score', 0)}) "
                f"| {change.get('source', '')} |"
            )
    else:
        lines.append("_No infrastructure changes were found in the correlation window._")

    lines += _source_availability(incident)

    lines += [
        "",
        "## Contributing Factors",
        "",
    ]
    lines += _bullets(
        incident.get("contributing_factors") or narrative.get("what_went_wrong"),
        "_No contributing factors were recorded._",
    )

    lines += [
        "",
        "## Resolution",
        "",
        f"- **Action:** {remediation.get('title') or 'No automated remediation was executed'}",
        f"- **Allowlist key:** `{remediation.get('action', 'n/a')}`",
        f"- **Risk:** {remediation.get('risk', 'n/a')}",
        f"- **Approved by:** {incident.get('approved_by') or 'not approved'}"
        f" at {incident.get('approved_at') or 'n/a'}",
        f"- **Target:** `{remediation.get('target', 'n/a')}`",
        f"- **Outcome:** {incident.get('remediation_status', 'n/a')}",
        "",
        "## Verification",
        "",
        f"- **Result:** {incident.get('verification_status', 'n/a')}",
        f"- **Basis:** {verification.get('reason', 'not recorded')}",
        f"- **Window:** {verification.get('window_seconds', 'n/a')} seconds",
        "",
    ]

    probes = [c for c in (verification.get("checks") or []) if c.get("kind") != "metrics"]
    if probes:
        lines.append("| Offset (s) | Healthy | Status | Latency (ms) | Alarm |")
        lines.append("| --- | --- | --- | --- | --- |")
        for check in probes:
            lines.append(
                f"| {check.get('offset_seconds', '')} "
                f"| {'yes' if check.get('healthy') else 'no'} "
                f"| {check.get('status_code', 'n/a')} "
                f"| {check.get('duration_ms', 'n/a')} "
                f"| {check.get('alarm_state', 'n/a')} |"
            )

    lines += ["", "## What Went Well", ""]
    lines += _bullets(narrative.get("what_went_well"), "_Nothing was recorded._")
    lines += ["", "## What Went Wrong", ""]
    lines += _bullets(narrative.get("what_went_wrong"), "_Nothing was recorded._")
    lines += ["", "## Preventive Actions", ""]
    lines += _bullets(narrative.get("preventive_actions"), "_None proposed._")
    lines += ["", "## Lessons Learned", ""]
    lines += _bullets(narrative.get("lessons_learned"), "_None recorded._")

    similar = incident.get("similar_incidents") or []
    lines += ["", "## Related Incidents", ""]
    if similar:
        for match in similar:
            lines.append(
                f"- **{match.get('incident_id', '')}** ({match.get('detected_at', '')}) - "
                f"{match.get('title', '')}. Resolution: {match.get('resolution', 'n/a')}. "
                f"Outcome: {match.get('outcome', 'n/a')}."
            )
    else:
        lines.append("_No previous incidents shared this failure signature._")

    lines += [
        "",
        "---",
        "",
        f"_Generated by OpsPilot on {models.iso()}. Narrative sections: "
        f"{_narrative_label(narrative_source)}. "
        f"All facts, timestamps and measurements are taken from the recorded "
        f"incident, not generated by a model._",
        "",
    ]
    return "\n".join(lines)


def _bullets(items: Any, empty: str) -> list[str]:
    """Render a list of strings as Markdown bullets."""
    if not items:
        return [empty]
    return [f"- {str(item)}" for item in items]


def _percent(value: Any) -> str:
    """Render a 0-1 confidence as a percentage."""
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "n/a"


def _narrative_label(source: str) -> str:
    """Describe how the narrative prose in this document was produced."""
    return {
        "bedrock": "written by Amazon Bedrock from the incident facts",
        "deterministic": "generated deterministically (Bedrock unavailable)",
    }.get(source, source or "unknown")


def _ai_label(ai_status: str) -> str:
    """Describe how the analysis was produced."""
    return {
        models.AIStatus.OK: "Amazon Bedrock analysis",
        models.AIStatus.FALLBACK: "Deterministic fallback (model output unusable)",
        models.AIStatus.UNAVAILABLE: "Deterministic fallback (Bedrock unavailable)",
        models.AIStatus.PENDING: "Analysis did not complete",
    }.get(ai_status, ai_status or "unknown")


def _source_availability(incident: dict[str, Any]) -> list[str]:
    """Render per-source evidence availability, including what was missing."""
    sources = incident.get("evidence_sources") or {}
    if not sources:
        return []
    lines = ["", "### Evidence source availability", "",
             "| Source | Available | Note |", "| --- | --- | --- |"]
    for name, meta in sources.items():
        if not isinstance(meta, dict):
            continue
        note = str(meta.get("note", "")).replace("|", "\\|")[:160]
        lines.append(
            f"| {name} | {'yes' if meta.get('available') else 'no'} | {note} |"
        )
    return lines
