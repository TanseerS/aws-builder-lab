"""API Lambda - the dashboard's single backend.

Every route reads real state from DynamoDB, S3 and CloudWatch. Nothing here is
mocked: if a value cannot be determined it is reported as unavailable rather
than invented.

Approval is the security-critical route. It re-validates the incident state and
the requested action against the allowlist *server side*, so a crafted request
cannot execute anything the investigation did not legitimately recommend.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from opspilot import config, dynamo, events, models, remediation_actions, responses
from opspilot.aws_clients import client
from opspilot.logging_utils import get_logger

log = get_logger("api")

DEMO_ALARMS: list[str] = json.loads(os.environ.get("DEMO_ALARMS", "[]"))
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

Handler = Callable[[dict[str, Any], dict[str, str]], dict[str, Any]]


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dispatch one API Gateway request."""
    method = _method(event)
    path = _path(event)
    log.bind(request_id=getattr(context, "aws_request_id", None), method=method, path=path)

    if method == "OPTIONS":
        return responses.respond(204, {})

    try:
        route, params = _match(method, path)
        if route is None:
            return responses.error(404, f"No route for {method} {path}", "route_not_found")
        return route(event, params)
    except (ClientError, BotoCoreError) as exc:
        log.error("aws_call_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        return responses.error(503, f"AWS dependency error: {type(exc).__name__}",
                               "dependency_error")
    except Exception as exc:  # noqa: BLE001 - the API must never leak a stack trace
        log.error("unhandled_api_error", error_type=type(exc).__name__, error=str(exc)[:300])
        return responses.server_error()


# --- Routes -------------------------------------------------------------------
def health(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /health - liveness plus a real dependency check."""
    checks: dict[str, Any] = {}
    healthy = True

    try:
        # Attribute access issues a real DescribeTable call, so this is a
        # genuine dependency check rather than a liveness placeholder.
        table_status = dynamo.incidents_table().table_status
        checks["incidents_table"] = {"available": True, "status": table_status}
    except (ClientError, BotoCoreError) as exc:
        checks["incidents_table"] = {"available": False, "error": type(exc).__name__}
        healthy = False

    return responses.ok(
        {
            "status": "healthy" if healthy else "degraded",
            "service": "opspilot",
            "environment": config.ENVIRONMENT,
            "region": config.AWS_REGION,
            "bedrock_model": config.BEDROCK_MODEL_ID,
            "checks": checks,
            "timestamp": models.iso(),
        }
    )


def list_incidents(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /incidents - newest first, optionally filtered by status."""
    query = event.get("queryStringParameters") or {}
    status = (query.get("status") or "").strip().upper() or None
    if status and status not in models.IncidentStatus.ALL:
        return responses.bad_request(
            f"Unknown status '{status}'. Valid values: {', '.join(models.IncidentStatus.ALL)}"
        )

    limit = _positive_int(query.get("limit"), DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
    incidents = dynamo.list_incidents(status=status, limit=limit)
    return responses.ok(
        [_summarise(incident) for incident in incidents],
        count=len(incidents),
        filter={"status": status, "limit": limit},
    )


def get_incident(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /incidents/{id} - the full incident record."""
    incident = dynamo.get_incident(params["id"])
    if incident is None:
        return responses.not_found(f"Incident {params['id']} not found")
    incident["actions_available"] = _available_actions(incident)
    return responses.ok(incident)


def approve(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """POST /incidents/{id}/approve - the human gate on remediation.

    Five checks run here before any event is published. Failing any one of them
    means nothing is executed.
    """
    incident_id = params["id"]
    body = _body(event)
    incident = dynamo.get_incident(incident_id)

    # 1. The incident exists.
    if incident is None:
        return responses.not_found(f"Incident {incident_id} not found")

    # 2. It is actually awaiting approval.
    if incident.get("status") != models.IncidentStatus.AWAITING_APPROVAL:
        return responses.conflict(
            f"Incident is {incident.get('status')}, not "
            f"{models.IncidentStatus.AWAITING_APPROVAL}",
            "invalid_state",
        )

    # 3. The action was one this investigation actually recommended.
    requested = str(body.get("action", "")).strip()
    recommended = [r for r in incident.get("recommendations", []) if r.get("executable")]
    if not recommended:
        return responses.conflict(
            "No executable remediation was recommended for this incident; "
            "manual remediation is required.",
            "manual_remediation_required",
        )

    if requested:
        # Resolve through the allowlist first so an operator may name the action
        # in any recognised phrasing. Unrecognised input resolves to None and is
        # refused below - it never falls through to a recommendation.
        requested_spec = remediation_actions.resolve_action(requested)
        chosen = next(
            (
                r
                for r in recommended
                if requested_spec is not None and r.get("action") == requested_spec.key
            ),
            None,
        )
        if chosen is None:
            return responses.bad_request(
                f"Action '{requested[:100]}' was not recommended for this incident",
                "action_not_recommended",
            )
    else:
        chosen = recommended[0]

    # 4. The action is allowlisted (re-checked server side, never trusted).
    spec = remediation_actions.resolve_action(chosen.get("action"))
    if spec is None:
        return responses.conflict(
            "The recommended action is not in the remediation allowlist",
            "action_not_allowlisted",
        )

    # 5. The target is a Demo Lab resource owned by this deployment.
    if not config.DEMO_FUNCTION_NAME.startswith(config.RESOURCE_PREFIX):
        return responses.conflict(
            "Remediation target is outside the OpsPilot Demo Lab", "target_out_of_scope"
        )

    approver = str(body.get("approved_by", "")).strip()[:120] or "dashboard-operator"
    now = models.iso()

    updated = dynamo.update_incident(
        incident_id,
        {
            "approved_action": spec.key,
            "approved_by": approver,
            "approved_at": now,
            "remediation_status": models.RemediationStatus.APPROVED,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        now,
                        f"Remediation approved by {approver}: {spec.title}",
                        models.TimelineKind.HUMAN,
                        source="human",
                    )
                ],
            ),
        },
    )

    published = events.publish(
        events.DetailType.REMEDIATION_APPROVED,
        {"incident_id": incident_id, "action": spec.key, "approved_by": approver},
    )
    if not published:
        return responses.error(
            503, "Approval recorded but remediation could not be dispatched", "dispatch_failed"
        )

    log.info("remediation_approved", incident_id=incident_id, action=spec.key,
             approved_by=approver)
    return responses.accepted(
        {
            "incident_id": incident_id,
            "action": spec.key,
            "title": spec.title,
            "risk": spec.risk,
            "status": updated.get("status"),
            "message": "Remediation approved and dispatched.",
        }
    )


def reject(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """POST /incidents/{id}/reject - decline the recommendation."""
    incident_id = params["id"]
    incident = dynamo.get_incident(incident_id)
    if incident is None:
        return responses.not_found(f"Incident {incident_id} not found")
    if incident.get("status") != models.IncidentStatus.AWAITING_APPROVAL:
        return responses.conflict(
            f"Incident is {incident.get('status')}, not awaiting approval", "invalid_state"
        )

    body = _body(event)
    rejector = str(body.get("rejected_by", "")).strip()[:120] or "dashboard-operator"
    reason = str(body.get("reason", "")).strip()[:500]
    now = models.iso()

    dynamo.transition(
        incident_id,
        models.IncidentStatus.FAILED,
        {
            "remediation_status": models.RemediationStatus.REJECTED,
            "verification_status": models.VerificationStatus.NOT_APPLICABLE,
            "rejection_reason": reason,
            "timeline": models.merge_timeline(
                incident.get("timeline"),
                [
                    models.timeline_entry(
                        now,
                        f"Remediation rejected by {rejector}",
                        models.TimelineKind.HUMAN,
                        source="human",
                        detail=reason,
                    )
                ],
            ),
        },
        expected_status=models.IncidentStatus.AWAITING_APPROVAL,
    )

    log.info("remediation_rejected", incident_id=incident_id, rejected_by=rejector)
    return responses.ok(
        {
            "incident_id": incident_id,
            "status": models.IncidentStatus.FAILED,
            "message": "Remediation rejected. Manual intervention is required.",
        }
    )


def reinvestigate(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """POST /incidents/{id}/reinvestigate - run the investigation again."""
    incident_id = params["id"]
    incident = dynamo.get_incident(incident_id)
    if incident is None:
        return responses.not_found(f"Incident {incident_id} not found")
    if incident.get("status") in (models.IncidentStatus.REMEDIATING,
                                  models.IncidentStatus.VERIFYING):
        return responses.conflict(
            f"Cannot reinvestigate while the incident is {incident.get('status')}",
            "invalid_state",
        )

    published = events.publish(
        events.DetailType.REINVESTIGATION_REQUESTED,
        {"incident_id": incident_id, "requested_at": models.iso()},
    )
    if not published:
        return responses.error(503, "Could not dispatch reinvestigation", "dispatch_failed")

    log.info("reinvestigation_requested", incident_id=incident_id)
    return responses.accepted(
        {"incident_id": incident_id, "message": "Reinvestigation dispatched."}
    )


def get_postmortem(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /incidents/{id}/postmortem - the stored Markdown document."""
    incident_id = params["id"]
    incident = dynamo.get_incident(incident_id)
    if incident is None:
        return responses.not_found(f"Incident {incident_id} not found")

    key = incident.get("postmortem_key") or ""
    if not key:
        return responses.error(
            404,
            "No postmortem has been generated for this incident yet. "
            "Postmortems are generated once an incident reaches a terminal state.",
            "postmortem_not_ready",
        )

    try:
        obj = client("s3").get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)
        markdown = obj["Body"].read().decode("utf-8")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return responses.not_found("Postmortem document is missing from S3")
        raise

    return responses.ok(
        {
            "incident_id": incident_id,
            "location": incident.get("postmortem_location", ""),
            "generated_at": incident.get("postmortem_generated_at", ""),
            "narrative_source": incident.get("postmortem_narrative_source", ""),
            "markdown": markdown,
        }
    )


def metrics_summary(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /metrics/summary - the dashboard's overview tiles."""
    incidents = dynamo.list_incidents(limit=MAX_LIST_LIMIT)
    today = models.utcnow().strftime("%Y-%m-%d")

    active = [i for i in incidents if i.get("status") in models.IncidentStatus.OPEN]
    today_all = [i for i in incidents if str(i.get("detected_at", "")).startswith(today)]
    resolved_today = [
        i for i in today_all if i.get("status") == models.IncidentStatus.RESOLVED
    ]

    durations = [
        d for d in (
            models.minutes_between(i.get("detected_at"), i.get("resolved_at"))
            for i in incidents
            if i.get("status") == models.IncidentStatus.RESOLVED and i.get("resolved_at")
        ) if d is not None
    ]
    mttr = round(sum(durations) / len(durations), 1) if durations else None

    auto_remediations = sum(
        1
        for i in incidents
        if i.get("remediation_status") == models.RemediationStatus.SUCCEEDED
    )
    verified = sum(
        1
        for i in incidents
        if i.get("verification_status") == models.VerificationStatus.VERIFIED
    )
    awaiting = [i for i in incidents if i.get("status") == models.IncidentStatus.AWAITING_APPROVAL]

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

    return responses.ok(
        {
            "active_incidents": len(active),
            "incidents_today": len(today_all),
            "resolved_today": len(resolved_today),
            "average_mttr_minutes": mttr,
            "auto_remediations": auto_remediations,
            "verified_recoveries": verified,
            "awaiting_approval": len(awaiting),
            "total_incidents": len(incidents),
            "system_healthy": not active and not firing,
            "alarms": alarm_states,
            "alarms_firing": firing,
            "bedrock_model": config.BEDROCK_MODEL_ID,
            "region": config.AWS_REGION,
            "environment": config.ENVIRONMENT,
            "generated_at": models.iso(),
        }
    )


def demo_inject(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """POST /demo/inject - start a controlled failure."""
    body = _body(event)
    scenario = str(body.get("scenario", "")).strip().lower()
    if not scenario:
        return responses.bad_request("Field 'scenario' is required", "missing_scenario")

    result = _invoke_demo_controller({"operation": "inject", "scenario": scenario})
    if not result.get("ok"):
        return responses.bad_request(
            result.get("error", "Injection failed"), "injection_failed"
        )
    return responses.accepted(result)


def demo_reset(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """POST /demo/reset - restore the demo environment to healthy."""
    result = _invoke_demo_controller({"operation": "reset"})
    if not result.get("ok"):
        return responses.error(503, result.get("error", "Reset failed"), "reset_failed")
    return responses.ok(result)


def demo_status(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    """GET /demo/status - live Demo Lab configuration and alarm states."""
    result = _invoke_demo_controller({"operation": "status"})
    if not result.get("ok"):
        return responses.error(503, result.get("error", "Status unavailable"),
                               "status_unavailable")
    return responses.ok(result)


# --- Routing ------------------------------------------------------------------
_STATIC_ROUTES: dict[tuple[str, str], Handler] = {
    ("GET", "/health"): health,
    ("GET", "/incidents"): list_incidents,
    ("GET", "/metrics/summary"): metrics_summary,
    ("POST", "/demo/inject"): demo_inject,
    ("POST", "/demo/reset"): demo_reset,
    ("GET", "/demo/status"): demo_status,
}

_INCIDENT_ROUTES: dict[tuple[str, str], Handler] = {
    ("GET", ""): get_incident,
    ("POST", "approve"): approve,
    ("POST", "reject"): reject,
    ("POST", "reinvestigate"): reinvestigate,
    ("GET", "postmortem"): get_postmortem,
}


def _match(method: str, path: str) -> tuple[Handler | None, dict[str, str]]:
    """Resolve a method and path to a handler plus path parameters."""
    normalised = "/" + path.strip("/")
    static = _STATIC_ROUTES.get((method, normalised))
    if static is not None:
        return static, {}

    segments = [s for s in normalised.split("/") if s]
    if len(segments) >= 2 and segments[0] == "incidents":
        incident_id = segments[1]
        suffix = segments[2] if len(segments) > 2 else ""
        handler = _INCIDENT_ROUTES.get((method, suffix))
        if handler is not None:
            return handler, {"id": incident_id}

    return None, {}


# --- Helpers ------------------------------------------------------------------
def _method(event: dict[str, Any]) -> str:
    """Extract the HTTP method from either payload format."""
    return (
        (event.get("requestContext", {}).get("http", {}) or {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def _path(event: dict[str, Any]) -> str:
    """Extract the request path, stripping any stage prefix."""
    raw = (
        event.get("rawPath")
        or (event.get("requestContext", {}).get("http", {}) or {}).get("path")
        or event.get("path")
        or "/"
    )
    stage = (event.get("requestContext", {}) or {}).get("stage", "")
    if stage and stage != "$default" and raw.startswith(f"/{stage}/"):
        raw = raw[len(stage) + 1 :]
    return raw


def _body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse a JSON request body, tolerating base64 and empty bodies."""
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_int(value: Any, default: int, maximum: int) -> int:
    """Parse a bounded positive integer from a query parameter."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _invoke_demo_controller(payload: dict[str, Any]) -> dict[str, Any]:
    """Call the Demo Lab controller synchronously.

    The Demo Lab runs under its own IAM role, so the API role never holds
    permission to modify the demo function itself.
    """
    if not config.DEMO_CONTROLLER_FUNCTION:
        return {"ok": False, "error": "Demo Lab controller is not configured"}
    try:
        response = client("lambda").invoke(
            FunctionName=config.DEMO_CONTROLLER_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        raw = response["Payload"].read().decode("utf-8")
        if response.get("FunctionError"):
            log.error("demo_controller_error", detail=raw[:300])
            return {"ok": False, "error": "Demo Lab controller failed"}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": "Bad response"}
    except (ClientError, BotoCoreError) as exc:
        log.error("demo_controller_unreachable", error_type=type(exc).__name__)
        return {"ok": False, "error": f"Demo Lab unreachable: {type(exc).__name__}"}
    except (json.JSONDecodeError, KeyError):
        return {"ok": False, "error": "Demo Lab returned an unreadable response"}


def _summarise(incident: dict[str, Any]) -> dict[str, Any]:
    """Reduce an incident to the columns the incident table renders."""
    root_cause = incident.get("root_cause") or {}
    return {
        "incident_id": incident.get("incident_id", ""),
        "status": incident.get("status", ""),
        "severity": incident.get("severity", ""),
        "title": incident.get("title", ""),
        "affected_service": incident.get("affected_service", ""),
        "incident_type": incident.get("incident_type", ""),
        "detected_at": incident.get("detected_at", ""),
        "resolved_at": incident.get("resolved_at", ""),
        "root_cause": (root_cause.get("description") or "")[:220],
        "confidence": root_cause.get("confidence", 0),
        "ai_status": incident.get("ai_status", ""),
        "remediation_status": incident.get("remediation_status", ""),
        "verification_status": incident.get("verification_status", ""),
        "change_summary": incident.get("change_summary", ""),
        "has_postmortem": bool(incident.get("postmortem_key")),
        "time_to_resolve_minutes": incident.get("time_to_resolve_minutes"),
    }


def _available_actions(incident: dict[str, Any]) -> list[dict[str, Any]]:
    """List the actions an operator may approve right now."""
    if incident.get("status") != models.IncidentStatus.AWAITING_APPROVAL:
        return []
    return [
        {
            "action": r.get("action"),
            "title": r.get("title"),
            "risk": r.get("risk"),
            "reason": r.get("reason"),
        }
        for r in incident.get("recommendations", [])
        if r.get("executable")
    ]
