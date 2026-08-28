"""Traffic generator - keeps a steady baseline of demo-app requests.

CloudWatch alarms need continuous datapoints to evaluate against. A scheduled
trickle of requests means the demo application always has a live metric
baseline, so an injected failure is detected in minutes rather than whenever
someone next happens to call the service.

One invocation per minute sits comfortably inside the Lambda free tier.
"""

from __future__ import annotations

import json
import os
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from opspilot import config
from opspilot.aws_clients import client
from opspilot.logging_utils import get_logger

log = get_logger("traffic_generator")

REQUESTS_PER_RUN = int(os.environ.get("REQUESTS_PER_RUN", "4"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Send a small burst of requests to the demo application."""
    if not config.DEMO_FUNCTION_NAME:
        log.error("demo_function_not_configured")
        return {"invoked": 0, "reason": "demo function not configured"}

    payload = json.dumps(
        {
            "rawPath": "/demo/app",
            "requestContext": {"http": {"method": "GET"}},
            "opspilot_synthetic": True,
        }
    ).encode("utf-8")

    invoked = 0
    for _ in range(max(1, REQUESTS_PER_RUN)):
        try:
            client("lambda").invoke(
                FunctionName=config.DEMO_FUNCTION_NAME,
                InvocationType="Event",
                Payload=payload,
            )
            invoked += 1
        except (ClientError, BotoCoreError) as exc:
            log.warning("synthetic_request_failed", error=str(exc)[:200])
            break

    log.info("synthetic_traffic_sent", invoked=invoked)
    return {"invoked": invoked}
