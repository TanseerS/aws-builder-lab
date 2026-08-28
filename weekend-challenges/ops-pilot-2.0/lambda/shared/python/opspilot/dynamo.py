"""DynamoDB persistence for incidents and the OpsPilot change log."""

from __future__ import annotations

from decimal import Decimal
from collections.abc import Iterable
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config, models
from .aws_clients import table
from .logging_utils import get_logger

log = get_logger("dynamo")

#: Attributes that must never be overwritten by a partial update.
_IMMUTABLE = frozenset({"incident_id", "dedupe_key", "detected_at"})


def to_dynamo(value: Any) -> Any:
    """Convert Python values into DynamoDB-safe types (floats -> Decimal)."""
    if isinstance(value, float):
        # str() round-trip avoids Decimal(float) precision noise.
        return Decimal(str(round(value, 6)))
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    if isinstance(value, str) and value == "":
        # Empty strings are legal in DynamoDB but noisy; keep them for the UI.
        return value
    return value


def from_dynamo(value: Any) -> Any:
    """Convert DynamoDB types back into JSON-serialisable Python values."""
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [from_dynamo(v) for v in value]
    return value


# --- Incidents ----------------------------------------------------------------
def incidents_table() -> Any:
    """Return the incidents table resource."""
    return table(config.INCIDENTS_TABLE)


def put_incident_if_new(item: dict[str, Any]) -> bool:
    """Insert an incident, returning False if it already exists.

    The caller derives ``incident_id`` deterministically from the alarm dedupe
    key (see :func:`models.new_incident_id`), so this single conditional write
    is a genuine atomic idempotency guard: a replayed EventBridge delivery
    resolves to the same primary key and loses the race.
    """
    try:
        incidents_table().put_item(
            Item=to_dynamo(item),
            ConditionExpression="attribute_not_exists(incident_id)",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise



def get_incident(incident_id: str) -> dict[str, Any] | None:
    """Fetch a single incident by id."""
    response = incidents_table().get_item(Key={"incident_id": incident_id})
    item = response.get("Item")
    return from_dynamo(item) if item else None


def update_incident(incident_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update and stamp ``updated_at``.

    Immutable identity attributes are stripped rather than rejected so callers
    can pass a whole incident dict without ceremony.
    """
    payload = {k: v for k, v in updates.items() if k not in _IMMUTABLE}
    payload["updated_at"] = models.iso()

    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    assignments: list[str] = []
    for index, (key, value) in enumerate(payload.items()):
        name_ref, value_ref = f"#f{index}", f":v{index}"
        names[name_ref] = key
        values[value_ref] = to_dynamo(value)
        assignments.append(f"{name_ref} = {value_ref}")

    response = incidents_table().update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return from_dynamo(response.get("Attributes", {}))


def transition(
    incident_id: str,
    target_status: str,
    updates: dict[str, Any] | None = None,
    expected_status: str | Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Move an incident to ``target_status`` under an optional state guard.

    Returns None when the guard fails, which is how concurrent workers avoid
    double-driving the same incident.
    """
    payload = dict(updates or {})
    payload["status"] = target_status
    payload["updated_at"] = models.iso()

    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    assignments: list[str] = []
    for index, (key, value) in enumerate(payload.items()):
        if key in _IMMUTABLE:
            continue
        name_ref, value_ref = f"#f{index}", f":v{index}"
        names[name_ref] = key
        values[value_ref] = to_dynamo(value)
        assignments.append(f"{name_ref} = {value_ref}")

    kwargs: dict[str, Any] = {
        "Key": {"incident_id": incident_id},
        "UpdateExpression": "SET " + ", ".join(assignments),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }

    if expected_status is not None:
        allowed = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status)
        names["#st"] = "status"
        placeholders = []
        for index, state in enumerate(allowed):
            ref = f":exp{index}"
            values[ref] = state
            placeholders.append(ref)
        kwargs["ConditionExpression"] = f"#st IN ({', '.join(placeholders)})"

    try:
        response = incidents_table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            log.warning(
                "transition_rejected",
                incident_id=incident_id,
                target_status=target_status,
                expected_status=list(allowed) if expected_status is not None else None,
            )
            return None
        raise

    log.info("incident_transition", incident_id=incident_id, status=target_status)
    return from_dynamo(response.get("Attributes", {}))


def query_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return incidents in a given status, newest first."""
    response = incidents_table().query(
        IndexName=config.STATUS_INDEX,
        KeyConditionExpression=Key("status").eq(status),
        ScanIndexForward=False,
        Limit=max(1, min(limit, 200)),
    )
    return [from_dynamo(item) for item in response.get("Items", [])]


def list_incidents(
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List incidents, optionally filtered by status, newest first."""
    if status:
        return query_by_status(status, limit)

    collected: list[dict[str, Any]] = []
    per_status = max(5, limit)
    for state in models.IncidentStatus.ALL:
        try:
            collected.extend(query_by_status(state, per_status))
        except ClientError as exc:  # a single hot partition must not break the list
            log.warning("status_query_failed", status=state, error=str(exc)[:200])
    collected.sort(key=lambda i: str(i.get("detected_at", "")), reverse=True)
    return collected[:limit]


def find_similar_incidents(
    incident: dict[str, Any],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Recall past incidents that share this incident's failure signature.

    Deterministic retrieval on (affected_service, incident_type) - deliberately
    no embeddings and no vector database. Falls back to the alarm name so a
    renamed service still matches.
    """
    incident_id = incident.get("incident_id", "")
    sig = incident.get("signature") or models.signature(
        incident.get("affected_service", ""), incident.get("incident_type", "")
    )

    matches: list[dict[str, Any]] = []
    seen: set[str] = {incident_id}

    def _collect(items: Iterable[dict[str, Any]]) -> None:
        for item in items:
            candidate_id = item.get("incident_id", "")
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            matches.append(item)

    try:
        response = incidents_table().query(
            IndexName=config.SIGNATURE_INDEX,
            KeyConditionExpression=Key("signature").eq(sig),
            ScanIndexForward=False,
            Limit=limit + 5,
        )
        _collect(from_dynamo(item) for item in response.get("Items", []))
    except ClientError as exc:
        log.warning("similar_signature_query_failed", error=str(exc)[:200])

    if len(matches) < limit:
        alarm = incident.get("alarm_name", "")
        for state in (models.IncidentStatus.RESOLVED, models.IncidentStatus.FAILED):
            if len(matches) >= limit:
                break
            try:
                _collect(
                    item
                    for item in query_by_status(state, 25)
                    if item.get("alarm_name") == alarm
                )
            except ClientError:
                continue

    return [summarise_for_recall(m) for m in matches[:limit]]


def summarise_for_recall(incident: dict[str, Any]) -> dict[str, Any]:
    """Condense a historical incident into the few fields recall needs."""
    root_cause = incident.get("root_cause") or {}
    return {
        "incident_id": incident.get("incident_id", ""),
        "title": incident.get("title", ""),
        "detected_at": incident.get("detected_at", ""),
        "status": incident.get("status", ""),
        "severity": incident.get("severity", ""),
        "incident_type": incident.get("incident_type", ""),
        "root_cause": (root_cause.get("description") or "")[:400],
        "resolution": incident.get("approved_action", "") or "No remediation recorded",
        "outcome": (
            "Resolved successfully"
            if incident.get("status") == models.IncidentStatus.RESOLVED
            else f"Ended in {incident.get('status', 'UNKNOWN')}"
        ),
        "verification_status": incident.get("verification_status", ""),
        "time_to_resolve_minutes": models.minutes_between(
            incident.get("detected_at"), incident.get("resolved_at")
        ),
    }


# --- Change log ---------------------------------------------------------------
#: Single partition key for the change log: the volume is tiny and a scan-free
#: time-range query is what change correlation actually needs.
CHANGE_SCOPE = "GLOBAL"


def record_change(change: dict[str, Any], ttl_days: int = 30) -> None:
    """Append an entry to the OpsPilot change log.

    The change log captures deployment and configuration changes at the instant
    they happen, which is what makes sub-minute change correlation possible;
    CloudTrail supplies the durable, independent record.
    """
    from datetime import timedelta

    item = dict(change)
    item.setdefault("timestamp", models.iso())
    item["scope"] = CHANGE_SCOPE
    item["ttl"] = int((models.utcnow() + timedelta(days=ttl_days)).timestamp())
    try:
        table(config.CHANGES_TABLE).put_item(Item=to_dynamo(item))
    except ClientError as exc:
        # A missing change-log write must never break failure injection.
        log.warning("change_log_write_failed", error=str(exc)[:200])


def recent_changes(since_iso: str, until_iso: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return change-log entries within a time window, oldest first."""
    try:
        response = table(config.CHANGES_TABLE).query(
            IndexName=config.CHANGES_INDEX,
            KeyConditionExpression=Key("scope").eq(CHANGE_SCOPE)
            & Key("timestamp").between(since_iso, until_iso),
            ScanIndexForward=True,
            Limit=max(1, min(limit, 200)),
        )
        return [from_dynamo(item) for item in response.get("Items", [])]
    except ClientError as exc:
        log.warning("change_log_query_failed", error=str(exc)[:200])
        return []
