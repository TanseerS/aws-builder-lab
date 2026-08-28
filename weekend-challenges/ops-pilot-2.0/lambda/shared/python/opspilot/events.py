"""EventBridge publication for OpsPilot lifecycle transitions.

Functions are coupled through events rather than direct invocation, so any
stage can be replaced or replayed independently.
"""

from __future__ import annotations

import json
from typing import Any, Final

from botocore.exceptions import ClientError

from . import config
from .aws_clients import client
from .logging_utils import get_logger

log = get_logger("events")


class DetailType:
    """The internal OpsPilot event vocabulary."""

    INCIDENT_DETECTED: Final[str] = "OpsPilot Incident Detected"
    INVESTIGATION_COMPLETED: Final[str] = "OpsPilot Investigation Completed"
    REMEDIATION_APPROVED: Final[str] = "OpsPilot Remediation Approved"
    REMEDIATION_COMPLETED: Final[str] = "OpsPilot Remediation Completed"
    VERIFICATION_COMPLETED: Final[str] = "OpsPilot Verification Completed"
    REINVESTIGATION_REQUESTED: Final[str] = "OpsPilot Reinvestigation Requested"
    POSTMORTEM_GENERATED: Final[str] = "OpsPilot Postmortem Generated"


def publish(detail_type: str, detail: dict[str, Any]) -> bool:
    """Publish one event to the OpsPilot bus.

    Returns False instead of raising: a failed event publication is logged and
    surfaced, but never crashes the stage that produced it.
    """
    if not config.EVENT_BUS_NAME:
        log.error("event_bus_not_configured", detail_type=detail_type)
        return False
    try:
        response = client("events").put_events(
            Entries=[
                {
                    "Source": config.EVENT_SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail, default=str),
                    "EventBusName": config.EVENT_BUS_NAME,
                }
            ]
        )
    except ClientError as exc:
        log.error("event_publish_failed", detail_type=detail_type, error=str(exc)[:300])
        return False

    if response.get("FailedEntryCount", 0):
        log.error(
            "event_publish_rejected",
            detail_type=detail_type,
            entries=response.get("Entries", []),
        )
        return False

    log.info(
        "event_published",
        detail_type=detail_type,
        incident_id=detail.get("incident_id"),
    )
    return True
