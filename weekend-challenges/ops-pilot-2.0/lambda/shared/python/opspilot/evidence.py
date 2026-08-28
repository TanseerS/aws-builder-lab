"""Deterministic evidence collection from AWS.

Every function here is bounded and independently fault-tolerant: a failure in
one source degrades that source only, and is reported honestly to the operator
rather than silently dropped. No AI is involved in this module - Lambda gathers
facts, Bedrock only interprets them later.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from . import config, models
from .aws_clients import client
from .logging_utils import get_logger

log = get_logger("evidence")

#: Log lines matching these tokens are treated as high-signal.
_ERROR_TOKENS = (
    "error", "exception", "traceback", "failed", "failure", "timeout",
    "throttl", "denied", "refused", "fatal", "critical", "5xx", "panic",
)


class EvidenceResult(dict):
    """A collected evidence source plus whether the collection succeeded.

    Carrying availability alongside the data is what lets the UI say
    "CloudTrail evidence unavailable" instead of implying nothing happened.
    """

    @classmethod
    def ok(cls, items: Any, **meta: Any) -> EvidenceResult:
        return cls(available=True, items=items, error="", **meta)

    @classmethod
    def unavailable(cls, reason: str, **meta: Any) -> EvidenceResult:
        return cls(available=False, items=[], error=reason[:300], **meta)


# --- CloudWatch alarms --------------------------------------------------------
def get_alarm(alarm_name: str) -> EvidenceResult:
    """Fetch an alarm's current configuration and state."""
    if not alarm_name:
        return EvidenceResult.unavailable("No alarm name on incident")
    try:
        response = client("cloudwatch").describe_alarms(AlarmNames=[alarm_name])
    except (ClientError, BotoCoreError) as exc:
        log.warning("alarm_lookup_failed", alarm_name=alarm_name, error=str(exc)[:200])
        return EvidenceResult.unavailable(f"Alarm lookup failed: {type(exc).__name__}")

    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return EvidenceResult.unavailable(f"Alarm {alarm_name} not found")

    alarm = alarms[0]
    return EvidenceResult.ok(
        {
            "alarm_name": alarm.get("AlarmName", ""),
            "state": alarm.get("StateValue", ""),
            "state_reason": alarm.get("StateReason", "")[:600],
            "state_updated_at": models.iso(alarm.get("StateUpdatedTimestamp")),
            "metric_name": alarm.get("MetricName", ""),
            "namespace": alarm.get("Namespace", ""),
            "statistic": alarm.get("Statistic", ""),
            "threshold": alarm.get("Threshold"),
            "comparison": alarm.get("ComparisonOperator", ""),
            "period_seconds": alarm.get("Period"),
            "evaluation_periods": alarm.get("EvaluationPeriods"),
            "description": alarm.get("AlarmDescription", "")[:400],
            "dimensions": {
                d.get("Name", ""): d.get("Value", "") for d in alarm.get("Dimensions", [])
            },
        }
    )


def get_alarm_states(alarm_names: list[str]) -> dict[str, str]:
    """Return {alarm_name: state} for a batch of alarms."""
    if not alarm_names:
        return {}
    try:
        response = client("cloudwatch").describe_alarms(AlarmNames=alarm_names[:100])
    except (ClientError, BotoCoreError) as exc:
        log.warning("alarm_batch_lookup_failed", error=str(exc)[:200])
        return {}
    return {a.get("AlarmName", ""): a.get("StateValue", "") for a in response.get("MetricAlarms", [])}


# --- CloudWatch metrics -------------------------------------------------------
def get_metric_series(
    namespace: str,
    metric_name: str,
    dimensions: dict[str, str],
    start: datetime,
    end: datetime,
    statistic: str = "Sum",
    period_seconds: int = 60,
) -> EvidenceResult:
    """Fetch one bounded metric series via GetMetricData."""
    if not namespace or not metric_name:
        return EvidenceResult.unavailable("Metric not specified")
    query = {
        "Id": "m1",
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric_name,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
            },
            "Period": period_seconds,
            "Stat": statistic,
        },
        "ReturnData": True,
    }
    try:
        response = client("cloudwatch").get_metric_data(
            MetricDataQueries=[query],
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
            MaxDatapoints=config.MAX_METRIC_POINTS,
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning(
            "metric_query_failed",
            namespace=namespace,
            metric_name=metric_name,
            error=str(exc)[:200],
        )
        return EvidenceResult.unavailable(f"Metric query failed: {type(exc).__name__}")

    results = response.get("MetricDataResults", [])
    if not results:
        return EvidenceResult.ok([], metric=metric_name, statistic=statistic)

    series = results[0]
    points = [
        {"timestamp": models.iso(ts), "value": float(val)}
        # strict=False: a malformed CloudWatch response should degrade this
        # one metric, not raise inside evidence collection.
        for ts, val in zip(
            series.get("Timestamps", []), series.get("Values", []), strict=False
        )
    ]
    points.sort(key=lambda p: p["timestamp"])
    return EvidenceResult.ok(
        points[-config.MAX_METRIC_POINTS :],
        metric=metric_name,
        namespace=namespace,
        statistic=statistic,
        dimensions=dimensions,
    )


def summarise_series(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a metric series to the handful of numbers a prompt needs."""
    values = [p["value"] for p in points if isinstance(p.get("value"), (int, float))]
    if not values:
        return {"count": 0, "sum": 0, "max": 0, "min": 0, "avg": 0, "latest": 0}
    return {
        "count": len(values),
        "sum": round(sum(values), 3),
        "max": round(max(values), 3),
        "min": round(min(values), 3),
        "avg": round(sum(values) / len(values), 3),
        "latest": round(values[-1], 3),
    }


# --- CloudWatch Logs ----------------------------------------------------------
def get_recent_logs(
    log_group: str,
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> EvidenceResult:
    """Fetch recent log events, prioritising error-bearing lines.

    Whole log streams are never collected: the newest ``MAX_LOG_EVENTS`` are
    read and then ranked so errors and lines nearest the incident survive
    truncation. This bounds both cost and prompt size.
    """
    if not log_group:
        return EvidenceResult.unavailable("No log group specified")

    cap = limit or config.MAX_LOG_EVENTS
    try:
        response = client("logs").filter_log_events(
            logGroupName=log_group,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=min(cap * 3, 1000),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return EvidenceResult.unavailable(f"Log group {log_group} does not exist yet")
        log.warning("log_query_failed", log_group=log_group, error=str(exc)[:200])
        return EvidenceResult.unavailable(f"Log query failed: {code or type(exc).__name__}")
    except BotoCoreError as exc:
        return EvidenceResult.unavailable(f"Log query failed: {type(exc).__name__}")

    events = [
        {
            "timestamp": models.iso(datetime.fromtimestamp(e["timestamp"] / 1000, tz=start.tzinfo)),
            "message": str(e.get("message", "")).strip()[:800],
            "stream": e.get("logStreamName", ""),
        }
        for e in response.get("events", [])
        if e.get("timestamp")
    ]
    prioritised = _prioritise_logs(events, cap)
    return EvidenceResult.ok(
        prioritised,
        log_group=log_group,
        total_seen=len(events),
        returned=len(prioritised),
        truncated=len(events) > len(prioritised),
    )


def _prioritise_logs(events: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Keep error lines first, then the most recent, then restore time order."""
    if len(events) <= cap:
        return sorted(events, key=lambda e: e["timestamp"])

    def is_signal(event: dict[str, Any]) -> bool:
        lowered = event["message"].lower()
        return any(token in lowered for token in _ERROR_TOKENS)

    signal = [e for e in events if is_signal(e)]
    noise = [e for e in events if not is_signal(e)]
    signal.sort(key=lambda e: e["timestamp"], reverse=True)
    noise.sort(key=lambda e: e["timestamp"], reverse=True)

    selected = signal[:cap]
    if len(selected) < cap:
        selected.extend(noise[: cap - len(selected)])
    return sorted(selected, key=lambda e: e["timestamp"])


# --- CloudTrail ---------------------------------------------------------------
#: Control-plane operations that plausibly change how a service behaves.
_CHANGE_EVENT_NAMES = (
    "UpdateFunctionConfiguration", "UpdateFunctionCode", "PublishVersion",
    "UpdateAlias", "CreateFunction", "DeleteFunction", "TagResource",
    "PutFunctionConcurrency", "DeleteFunctionConcurrency",
    "UpdateTable", "CreateTable", "DeleteTable", "UpdateTimeToLive",
    "PutScalingPolicy", "RegisterScalableTarget",
    "PutRolePolicy", "AttachRolePolicy", "DetachRolePolicy", "DeleteRolePolicy",
    "UpdateAssumeRolePolicy", "CreateRole", "DeleteRole", "PutUserPolicy",
    "CreateDeployment", "UpdateStage", "UpdateRestApi", "UpdateApi",
    "UpdateIntegration", "UpdateRoute", "CreateStage",
    "PutBucketPolicy", "PutBucketVersioning", "PutBucketEncryption",
    "DeleteBucketPolicy", "PutBucketPublicAccessBlock",
    "PutRule", "PutTargets", "RemoveTargets", "DeleteRule",
    "PutMetricAlarm", "DeleteAlarms", "SetAlarmState", "DisableAlarmActions",
    "CreateStack", "UpdateStack", "DeleteStack", "ExecuteChangeSet",
    "UpdateSecret", "PutParameter", "DeleteParameter",
)


def get_cloudtrail_changes(
    start: datetime,
    end: datetime,
    limit: int | None = None,
) -> EvidenceResult:
    """Look up recent control-plane changes from CloudTrail event history.

    Uses ``LookupEvents`` against the free 90-day event history so no trail is
    strictly required. CloudTrail delivery is not instantaneous - see
    ``docs/architecture.md`` - so this is deliberately one of *two* change
    sources, alongside OpsPilot's own change log.
    """
    cap = limit or config.MAX_CLOUDTRAIL_EVENTS
    collected: list[dict[str, Any]] = []
    try:
        paginator = client("cloudtrail").get_paginator("lookup_events")
        pages = paginator.paginate(
            StartTime=start,
            EndTime=end,
            LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
            PaginationConfig={"MaxItems": cap * 2, "PageSize": 50},
        )
        for page in pages:
            for raw in page.get("Events", []):
                normalised = _normalise_cloudtrail_event(raw)
                if normalised:
                    collected.append(normalised)
            if len(collected) >= cap * 2:
                break
    except (ClientError, BotoCoreError) as exc:
        log.warning("cloudtrail_lookup_failed", error=str(exc)[:200])
        return EvidenceResult.unavailable(f"CloudTrail unavailable: {type(exc).__name__}")

    relevant = [c for c in collected if c["action"] in _CHANGE_EVENT_NAMES]
    chosen = (relevant or collected)[:cap]
    chosen.sort(key=lambda c: c["timestamp"])
    return EvidenceResult.ok(
        chosen,
        total_seen=len(collected),
        returned=len(chosen),
        truncated=len(collected) > len(chosen),
    )


def _normalise_cloudtrail_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a CloudTrail event into OpsPilot's common change shape."""
    import json

    event_name = raw.get("EventName", "")
    if not event_name:
        return None

    resources = raw.get("Resources", []) or []
    resource_name = resources[0].get("ResourceName", "") if resources else ""

    detail = ""
    try:
        payload = json.loads(raw.get("CloudTrailEvent", "{}"))
        source = payload.get("eventSource", "").split(".")[0]
        params = payload.get("requestParameters") or {}
        if not resource_name:
            resource_name = str(
                params.get("functionName")
                or params.get("tableName")
                or params.get("bucketName")
                or params.get("name")
                or ""
            )
        if params:
            detail = json.dumps(params, default=str)[:400]
        if payload.get("errorCode"):
            detail = f"errorCode={payload['errorCode']} {detail}"[:400]
    except (ValueError, TypeError):
        source = raw.get("EventSource", "").split(".")[0]

    return {
        "timestamp": models.iso(raw.get("EventTime")),
        "service": source or raw.get("EventSource", "").split(".")[0] or "aws",
        "resource": str(resource_name)[:200],
        "action": event_name,
        "actor": str(raw.get("Username", "") or "unknown")[:120],
        "details": detail,
        "source": "cloudtrail",
        "event_id": raw.get("EventId", ""),
    }


# --- Demo application state ---------------------------------------------------
def get_function_state(function_name: str) -> EvidenceResult:
    """Read the live configuration of a Lambda function."""
    if not function_name:
        return EvidenceResult.unavailable("No function configured")
    try:
        response = client("lambda").get_function_configuration(FunctionName=function_name)
    except (ClientError, BotoCoreError) as exc:
        log.warning("function_state_failed", function_name=function_name, error=str(exc)[:200])
        return EvidenceResult.unavailable(f"Function lookup failed: {type(exc).__name__}")

    env_vars = (response.get("Environment") or {}).get("Variables", {}) or {}
    return EvidenceResult.ok(
        {
            "function_name": response.get("FunctionName", ""),
            "version": response.get("Version", ""),
            "runtime": response.get("Runtime", ""),
            "memory_mb": response.get("MemorySize"),
            "timeout_seconds": response.get("Timeout"),
            "last_modified": response.get("LastModified", ""),
            "state": response.get("State", ""),
            "last_update_status": response.get("LastUpdateStatus", ""),
            # Demo fault flags only - never secrets. See docs/architecture.md.
            "failure_mode": env_vars.get("FAILURE_MODE", "none"),
            "latency_ms": env_vars.get("LATENCY_MS", "0"),
            "error_rate": env_vars.get("ERROR_RATE", "0"),
            "config_profile": env_vars.get("CONFIG_PROFILE", "default"),
            "write_burst": env_vars.get("WRITE_BURST", "0"),
            "target_table": env_vars.get("TARGET_TABLE", ""),
        }
    )


def probe_demo_app(function_name: str) -> EvidenceResult:
    """Invoke the demo application's health path and report what happened.

    This is the strongest recovery signal available: it exercises the real
    service rather than waiting on metric aggregation.
    """
    import json

    if not function_name:
        return EvidenceResult.unavailable("No demo function configured")

    started = models.utcnow()
    try:
        response = client("lambda").invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"rawPath": "/demo/app", "requestContext": {"http": {"method": "GET"}},
                                "opspilot_probe": True}).encode("utf-8"),
        )
        payload_raw = response["Payload"].read().decode("utf-8")
        function_error = response.get("FunctionError", "")
    except (ClientError, BotoCoreError) as exc:
        log.warning("demo_probe_failed", error=str(exc)[:200])
        return EvidenceResult.unavailable(f"Probe invocation failed: {type(exc).__name__}")

    duration_ms = int((models.utcnow() - started).total_seconds() * 1000)
    status_code = 0
    body_summary = ""
    try:
        parsed = json.loads(payload_raw)
        status_code = int(parsed.get("statusCode", 0))
        body_summary = str(parsed.get("body", ""))[:300]
    except (ValueError, TypeError, KeyError):
        body_summary = payload_raw[:300]

    healthy = not function_error and status_code == 200
    return EvidenceResult.ok(
        {
            "healthy": healthy,
            "status_code": status_code,
            "function_error": function_error,
            "duration_ms": duration_ms,
            "response": body_summary,
        }
    )


# --- Windows ------------------------------------------------------------------
def incident_window(
    detected_at: str,
    lookback_minutes: int | None = None,
    forward_minutes: int = 5,
) -> tuple[datetime, datetime]:
    """Return the (start, end) window used for all evidence queries."""
    detected = models.parse_iso(detected_at) or models.utcnow()
    back = lookback_minutes if lookback_minutes is not None else config.EVIDENCE_WINDOW_MINUTES
    start = detected - timedelta(minutes=back)
    end = min(detected + timedelta(minutes=forward_minutes), models.utcnow())
    if end <= start:
        end = start + timedelta(minutes=1)
    return start, end


# --- Metric catalog -----------------------------------------------------------
#: Dimension-set key -> the CloudWatch dimension the demo resources use.
_DIMENSION_SETS = {
    "fn": ("FunctionName", "DEMO_FUNCTION_NAME"),
    "svc": ("Service", "DEMO_FUNCTION_NAME"),
    "tbl": ("TableName", "DEMO_TABLE_NAME"),
}


def load_metric_catalog(raw: str) -> dict[str, list[dict[str, Any]]]:
    """Expand Terraform's compact metric catalog into full probe specs.

    A Lambda's entire environment must fit in 4 KB, so Terraform ships the
    catalog with short keys and dimension *references* rather than repeating a
    dimension map on every probe. This rehydrates it:

        {"p": {"lambda_errors": {"ns": ..., "m": ..., "d": "fn", "e": true}},
         "c": {"lambda_error": ["lambda_errors", ...]}}

    Returns ``{incident_type: [probe, ...]}``. Malformed or missing input
    yields an empty catalog rather than raising - evidence collection then
    reports metrics as unavailable, which is honest.
    """
    import json

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        log.warning("metric_catalog_unparseable")
        return {}

    if not isinstance(parsed, dict):
        return {}

    probes = parsed.get("p") or {}
    scenarios = parsed.get("c") or {}
    if not isinstance(probes, dict) or not isinstance(scenarios, dict):
        return {}

    resolved: dict[str, dict[str, Any]] = {}
    for key, probe in probes.items():
        if not isinstance(probe, dict):
            continue
        dim_key, source_attr = _DIMENSION_SETS.get(probe.get("d", "fn"), _DIMENSION_SETS["fn"])
        resource = getattr(config, source_attr, "")
        if not resource:
            continue
        statistic = probe.get("s", "Sum")
        metric_name = probe.get("m", "")
        resolved[key] = {
            "namespace": probe.get("ns", ""),
            "metric_name": metric_name,
            "statistic": statistic,
            "dimensions": {dim_key: resource},
            "label": f"{metric_name} ({statistic})",
            "period": int(probe.get("period", 60)),
            "error_signal": bool(probe.get("e", False)),
        }

    catalog: dict[str, list[dict[str, Any]]] = {}
    for incident_type, keys in scenarios.items():
        if not isinstance(keys, list):
            continue
        catalog[incident_type] = [resolved[k] for k in keys if k in resolved]
    return catalog
