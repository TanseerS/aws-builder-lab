"""Incident data model, lifecycle states and helpers.

The incident record is the single source of truth for the whole platform; the
API, the dashboard and the postmortem all read from it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final


# --- Lifecycle ----------------------------------------------------------------
class IncidentStatus:
    """The OpsPilot incident state machine."""

    DETECTED: Final[str] = "DETECTED"
    INVESTIGATING: Final[str] = "INVESTIGATING"
    ROOT_CAUSE_IDENTIFIED: Final[str] = "ROOT_CAUSE_IDENTIFIED"
    AWAITING_APPROVAL: Final[str] = "AWAITING_APPROVAL"
    REMEDIATING: Final[str] = "REMEDIATING"
    VERIFYING: Final[str] = "VERIFYING"
    RESOLVED: Final[str] = "RESOLVED"
    FAILED: Final[str] = "FAILED"

    ALL: Final[tuple[str, ...]] = (
        DETECTED,
        INVESTIGATING,
        ROOT_CAUSE_IDENTIFIED,
        AWAITING_APPROVAL,
        REMEDIATING,
        VERIFYING,
        RESOLVED,
        FAILED,
    )
    #: States in which an incident is still consuming operator attention.
    OPEN: Final[tuple[str, ...]] = (
        DETECTED,
        INVESTIGATING,
        ROOT_CAUSE_IDENTIFIED,
        AWAITING_APPROVAL,
        REMEDIATING,
        VERIFYING,
    )
    TERMINAL: Final[tuple[str, ...]] = (RESOLVED, FAILED)


#: Legal state transitions. Anything else is rejected by ``can_transition``.
TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    IncidentStatus.DETECTED: (IncidentStatus.INVESTIGATING, IncidentStatus.FAILED),
    IncidentStatus.INVESTIGATING: (
        IncidentStatus.ROOT_CAUSE_IDENTIFIED,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.FAILED,
    ),
    IncidentStatus.ROOT_CAUSE_IDENTIFIED: (
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    ),
    IncidentStatus.AWAITING_APPROVAL: (
        IncidentStatus.REMEDIATING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    ),
    IncidentStatus.REMEDIATING: (IncidentStatus.VERIFYING, IncidentStatus.FAILED),
    IncidentStatus.VERIFYING: (
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
        IncidentStatus.INVESTIGATING,
    ),
    IncidentStatus.RESOLVED: (IncidentStatus.INVESTIGATING,),
    IncidentStatus.FAILED: (IncidentStatus.INVESTIGATING,),
}


def can_transition(current: str, target: str) -> bool:
    """Return True if ``current -> target`` is a legal lifecycle transition."""
    if current == target:
        return True
    return target in TRANSITIONS.get(current, ())


class Severity:
    """Incident severity levels, ordered most to least urgent."""

    CRITICAL: Final[str] = "CRITICAL"
    HIGH: Final[str] = "HIGH"
    MEDIUM: Final[str] = "MEDIUM"
    LOW: Final[str] = "LOW"
    UNKNOWN: Final[str] = "UNKNOWN"

    ALL: Final[tuple[str, ...]] = (CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN)
    RANK: Final[dict[str, int]] = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, UNKNOWN: 4}

    @classmethod
    def normalise(cls, value: Any, default: str = MEDIUM) -> str:
        """Coerce arbitrary model output into a known severity."""
        if not isinstance(value, str):
            return default
        candidate = value.strip().upper()
        if candidate in cls.ALL:
            return candidate
        aliases = {
            "SEV1": cls.CRITICAL,
            "SEV-1": cls.CRITICAL,
            "P1": cls.CRITICAL,
            "SEV2": cls.HIGH,
            "P2": cls.HIGH,
            "SEV3": cls.MEDIUM,
            "P3": cls.MEDIUM,
            "SEV4": cls.LOW,
            "P4": cls.LOW,
            "WARNING": cls.MEDIUM,
            "MINOR": cls.LOW,
            "MAJOR": cls.HIGH,
            "SEVERE": cls.CRITICAL,
            "INFO": cls.LOW,
        }
        return aliases.get(candidate, default)


class VerificationStatus:
    """Outcome of the post-remediation recovery check."""

    PENDING: Final[str] = "PENDING"
    IN_PROGRESS: Final[str] = "IN_PROGRESS"
    VERIFIED: Final[str] = "VERIFIED"
    VERIFICATION_FAILED: Final[str] = "VERIFICATION_FAILED"
    NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"


class RemediationStatus:
    """Lifecycle of the remediation action itself."""

    NOT_STARTED: Final[str] = "NOT_STARTED"
    AWAITING_APPROVAL: Final[str] = "AWAITING_APPROVAL"
    APPROVED: Final[str] = "APPROVED"
    REJECTED: Final[str] = "REJECTED"
    IN_PROGRESS: Final[str] = "IN_PROGRESS"
    SUCCEEDED: Final[str] = "SUCCEEDED"
    FAILED: Final[str] = "FAILED"
    MANUAL_REQUIRED: Final[str] = "MANUAL_REQUIRED"


class AIStatus:
    """Whether the Bedrock analysis for this incident actually succeeded."""

    OK: Final[str] = "OK"
    FALLBACK: Final[str] = "FALLBACK"
    UNAVAILABLE: Final[str] = "UNAVAILABLE"
    PENDING: Final[str] = "PENDING"


# --- Time helpers -------------------------------------------------------------
def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """Render a UTC timestamp in the ISO-8601 form used everywhere in OpsPilot."""
    moment = dt or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> datetime | None:
    """Best-effort parse of the timestamp formats AWS services return."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def minutes_between(start: Any, end: Any) -> float | None:
    """Whole minutes between two timestamps, or None if either is unparseable."""
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 60.0, 2)


# --- Identity -----------------------------------------------------------------
def new_incident_id(now: datetime | None = None, seed: str | None = None) -> str:
    """Generate a human-readable, sortable incident id.

    When ``seed`` (a dedupe key) is supplied the id is *deterministic*, so a
    replayed alarm event resolves to the same primary key and DynamoDB's
    ``attribute_not_exists`` condition enforces idempotency atomically. Without
    a seed the id is random, which suits manually-opened incidents.
    """
    moment = now or utcnow()
    if seed:
        suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6].upper()
    else:
        suffix = uuid.uuid4().hex[:6].upper()
    return f"INC-{moment.strftime('%Y%m%d')}-{suffix}"


def dedupe_key(alarm_name: str, state_change_time: str, bucket_seconds: int = 300) -> str:
    """Build a deterministic key so one alarm flap yields exactly one incident.

    The state-change time is floored into a bucket so that near-simultaneous
    duplicate deliveries of the same alarm transition collapse onto one key,
    which a DynamoDB conditional write then enforces.
    """
    moment = parse_iso(state_change_time) or utcnow()
    epoch = int(moment.timestamp())
    bucket = epoch - (epoch % max(bucket_seconds, 1))
    raw = f"{alarm_name}|{bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def signature(affected_service: str, incident_type: str) -> str:
    """Deterministic similarity key: the same failure shape yields the same key.

    This is how OpsPilot recalls past incidents without embeddings or a vector
    database - see ``docs/architecture.md``.
    """
    return f"{(affected_service or 'unknown').lower()}|{(incident_type or 'unknown').lower()}"


# --- Timeline -----------------------------------------------------------------
class TimelineKind:
    """Timeline entry categories, used by the dashboard to pick an icon."""

    CHANGE: Final[str] = "change"
    METRIC: Final[str] = "metric"
    ALARM: Final[str] = "alarm"
    OPSPILOT: Final[str] = "opspilot"
    HUMAN: Final[str] = "human"
    REMEDIATION: Final[str] = "remediation"
    VERIFICATION: Final[str] = "verification"
    LOG: Final[str] = "log"


_KIND_ICONS: Final[dict[str, str]] = {
    TimelineKind.CHANGE: "\U0001f680",        # rocket
    TimelineKind.METRIC: "\U0001f4c8",        # chart increasing
    TimelineKind.ALARM: "\U0001f534",         # red circle
    TimelineKind.OPSPILOT: "\U0001f916",      # robot
    TimelineKind.HUMAN: "\U0001f464",         # bust in silhouette
    TimelineKind.REMEDIATION: "\U0001f527",   # wrench
    TimelineKind.VERIFICATION: "✅",      # check mark
    TimelineKind.LOG: "\U0001f4c4",           # page
}


def timeline_entry(
    timestamp: str,
    event: str,
    kind: str = TimelineKind.OPSPILOT,
    source: str = "opspilot",
    detail: str = "",
) -> dict[str, Any]:
    """Build one chronological timeline entry."""
    return {
        "timestamp": timestamp,
        "event": event,
        "kind": kind,
        "icon": _KIND_ICONS.get(kind, "•"),
        "source": source,
        "detail": detail,
    }


def merge_timeline(
    existing: list[dict[str, Any]] | None,
    additions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge and sort timeline entries, dropping exact duplicates."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in list(existing or []) + list(additions or []):
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("timestamp", "")), str(entry.get("event", "")))
        merged.setdefault(key, entry)
    return sorted(merged.values(), key=lambda e: str(e.get("timestamp", "")))


# --- Incident construction ----------------------------------------------------
@dataclass
class IncidentSeed:
    """The minimum facts needed to open an incident, all from the alarm event."""

    alarm_name: str
    affected_service: str
    incident_type: str
    severity: str
    title: str
    description: str
    detected_at: str
    source: str = "cloudwatch-alarm"
    alarm_reason: str = ""
    metric_namespace: str = ""
    metric_name: str = ""
    tags: dict[str, str] = field(default_factory=dict)


def build_incident(seed: IncidentSeed, incident_id: str, dedupe: str) -> dict[str, Any]:
    """Create the initial DynamoDB item for a newly detected incident."""
    now = seed.detected_at
    return {
        "incident_id": incident_id,
        "dedupe_key": dedupe,
        "status": IncidentStatus.DETECTED,
        "severity": seed.severity,
        "title": seed.title,
        "description": seed.description,
        "detected_at": now,
        "updated_at": now,
        "resolved_at": "",
        "source": seed.source,
        "alarm_name": seed.alarm_name,
        "alarm_reason": seed.alarm_reason,
        "affected_service": seed.affected_service,
        "incident_type": seed.incident_type,
        "signature": signature(seed.affected_service, seed.incident_type),
        "metric_namespace": seed.metric_namespace,
        "metric_name": seed.metric_name,
        "root_cause": {"description": "", "confidence": 0, "category": ""},
        "confidence": 0,
        "evidence": [],
        "contributing_factors": [],
        "timeline": [
            timeline_entry(
                now,
                f"CloudWatch alarm {seed.alarm_name} entered ALARM",
                TimelineKind.ALARM,
                source="cloudwatch",
                detail=seed.alarm_reason,
            ),
            timeline_entry(
                now, "OpsPilot opened incident", TimelineKind.OPSPILOT, source="opspilot"
            ),
        ],
        "changes": [],
        "recommendations": [],
        "approved_action": "",
        "approved_by": "",
        "approved_at": "",
        "remediation_status": RemediationStatus.NOT_STARTED,
        "remediation_detail": {},
        "verification_status": VerificationStatus.PENDING,
        "verification_detail": {},
        "postmortem_location": "",
        "postmortem_url": "",
        "similar_incidents": [],
        "ai_status": AIStatus.PENDING,
        "ai_summary": "",
        "evidence_sources": {},
        "investigation_count": 0,
        "ttl": int((utcnow() + timedelta(days=90)).timestamp()),
    }
