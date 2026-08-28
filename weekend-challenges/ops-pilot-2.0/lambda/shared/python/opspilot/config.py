"""Environment-driven configuration.

Every tunable is read from the environment so that Terraform owns all
configuration and nothing (model id, table name, region, ARN) is hard-coded in
Python.
"""

from __future__ import annotations

import os
from typing import Final


def env(name: str, default: str = "") -> str:
    """Return a string environment variable."""
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    """Return an int environment variable, falling back on malformed input."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Return a float environment variable, falling back on malformed input."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """Return a boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Identity -----------------------------------------------------------------
PROJECT: Final[str] = env("PROJECT_NAME", "opspilot")
ENVIRONMENT: Final[str] = env("ENVIRONMENT", "showcase")
RESOURCE_PREFIX: Final[str] = env("RESOURCE_PREFIX", f"{PROJECT}-{ENVIRONMENT}")
AWS_REGION: Final[str] = env("AWS_REGION") or env("AWS_DEFAULT_REGION", "us-east-1")
SERVICE_NAME: Final[str] = env("SERVICE_NAME", "opspilot")

# --- Storage ------------------------------------------------------------------
INCIDENTS_TABLE: Final[str] = env("INCIDENTS_TABLE")
CHANGES_TABLE: Final[str] = env("CHANGES_TABLE")
ARTIFACTS_BUCKET: Final[str] = env("ARTIFACTS_BUCKET")
STATUS_INDEX: Final[str] = env("STATUS_INDEX", "status-detected_at-index")
SIGNATURE_INDEX: Final[str] = env("SIGNATURE_INDEX", "signature-detected_at-index")
CHANGES_INDEX: Final[str] = env("CHANGES_INDEX", "scope-timestamp-index")

# --- Event bus ----------------------------------------------------------------
EVENT_BUS_NAME: Final[str] = env("EVENT_BUS_NAME")
EVENT_SOURCE: Final[str] = env("EVENT_SOURCE", "opspilot.core")

# --- Bedrock ------------------------------------------------------------------
BEDROCK_MODEL_ID: Final[str] = env("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
BEDROCK_MAX_TOKENS: Final[int] = env_int("BEDROCK_MAX_TOKENS", 2000)
BEDROCK_TEMPERATURE: Final[float] = env_float("BEDROCK_TEMPERATURE", 0.1)
BEDROCK_MAX_ATTEMPTS: Final[int] = env_int("BEDROCK_MAX_ATTEMPTS", 4)
BEDROCK_BASE_BACKOFF_SECONDS: Final[float] = env_float("BEDROCK_BASE_BACKOFF_SECONDS", 1.0)

# --- Evidence collection bounds ----------------------------------------------
MAX_LOG_EVENTS: Final[int] = env_int("MAX_LOG_EVENTS", 100)
MAX_CLOUDTRAIL_EVENTS: Final[int] = env_int("MAX_CLOUDTRAIL_EVENTS", 50)
MAX_METRIC_POINTS: Final[int] = env_int("MAX_METRIC_POINTS", 100)
MAX_PROMPT_CHARS: Final[int] = env_int("MAX_PROMPT_CHARS", 18000)
MAX_SIMILAR_INCIDENTS: Final[int] = env_int("MAX_SIMILAR_INCIDENTS", 3)
CHANGE_LOOKBACK_MINUTES: Final[int] = env_int("CHANGE_LOOKBACK_MINUTES", 15)
EVIDENCE_WINDOW_MINUTES: Final[int] = env_int("EVIDENCE_WINDOW_MINUTES", 20)

# --- Demo lab -----------------------------------------------------------------
DEMO_FUNCTION_NAME: Final[str] = env("DEMO_FUNCTION_NAME")
DEMO_FUNCTION_ARN: Final[str] = env("DEMO_FUNCTION_ARN")
DEMO_TABLE_NAME: Final[str] = env("DEMO_TABLE_NAME")
DEMO_CONTROLLER_FUNCTION: Final[str] = env("DEMO_CONTROLLER_FUNCTION")
DEMO_METRIC_NAMESPACE: Final[str] = env("DEMO_METRIC_NAMESPACE", "OpsPilot/DemoApp")

# --- Verification -------------------------------------------------------------
VERIFICATION_CHECKS: Final[int] = env_int("VERIFICATION_CHECKS", 6)
VERIFICATION_INTERVAL_SECONDS: Final[int] = env_int("VERIFICATION_INTERVAL_SECONDS", 30)
