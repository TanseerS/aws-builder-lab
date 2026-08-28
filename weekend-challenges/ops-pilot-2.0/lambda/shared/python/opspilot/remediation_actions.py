"""The remediation allowlist - OpsPilot's hard safety boundary.

Nothing the model returns is ever executed. The model produces a *label*; that
label is normalised and looked up in :data:`ALLOWED_ACTIONS`. A label with no
entry yields no action at all and the incident is marked as requiring manual
remediation. Resource targets come from Terraform-provided environment
variables, never from model output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final


class Risk:
    """Risk classification attached to each remediation action."""

    LOW: Final[str] = "LOW"
    MEDIUM: Final[str] = "MEDIUM"
    HIGH: Final[str] = "HIGH"


@dataclass(frozen=True)
class ActionSpec:
    """A single approved remediation operation."""

    key: str
    title: str
    description: str
    risk: str
    #: Failure scenarios this action is a valid response to.
    applies_to: tuple[str, ...]
    #: Demo-app environment overrides applied when the action runs. ``None``
    #: means "restore the Terraform baseline for this variable".
    env_overrides: dict[str, str | None]
    #: True when the action only relaxes/clears a fault flag (never destructive).
    destructive: bool = False


#: Every remediation OpsPilot is capable of performing. There is no code path
#: that executes anything outside this table.
ALLOWED_ACTIONS: Final[dict[str, ActionSpec]] = {
    "reset_demo_lambda": ActionSpec(
        key="reset_demo_lambda",
        title="Reset demo Lambda to healthy configuration",
        description=(
            "Clear every injected fault flag on the OpsPilot demo function and "
            "restore its Terraform baseline environment."
        ),
        risk=Risk.LOW,
        applies_to=("lambda_error", "lambda_latency", "application_error",
                    "database_throttle", "configuration_error", "unknown"),
        env_overrides={
            "FAILURE_MODE": None,
            "LATENCY_MS": None,
            "ERROR_RATE": None,
            "CONFIG_PROFILE": None,
            "TARGET_TABLE": None,
        },
    ),
    "reset_demo_error_mode": ActionSpec(
        key="reset_demo_error_mode",
        title="Clear demo Lambda error injection",
        description="Set the demo function's failure mode back to none.",
        risk=Risk.LOW,
        applies_to=("lambda_error", "application_error"),
        env_overrides={"FAILURE_MODE": None, "ERROR_RATE": None},
    ),
    "reset_demo_latency_mode": ActionSpec(
        key="reset_demo_latency_mode",
        title="Clear demo Lambda latency injection",
        description="Remove the artificial delay from the demo function.",
        risk=Risk.LOW,
        applies_to=("lambda_latency",),
        env_overrides={"FAILURE_MODE": None, "LATENCY_MS": None},
    ),
    "restore_demo_configuration": ActionSpec(
        key="restore_demo_configuration",
        title="Restore demo application configuration",
        description=(
            "Reapply the Terraform-managed configuration profile and table "
            "target to the demo function."
        ),
        risk=Risk.LOW,
        applies_to=("configuration_error", "application_error"),
        env_overrides={"CONFIG_PROFILE": None, "TARGET_TABLE": None, "FAILURE_MODE": None},
    ),
    "restore_previous_demo_version": ActionSpec(
        key="restore_previous_demo_version",
        title="Restore the previous demo function configuration",
        description=(
            "Roll the demo function back to the last configuration snapshot "
            "recorded in the OpsPilot change log before the incident began."
        ),
        risk=Risk.MEDIUM,
        applies_to=("lambda_error", "lambda_latency", "configuration_error",
                    "application_error", "database_throttle", "unknown"),
        env_overrides={},  # resolved at run time from the change log
    ),
    "reset_demo_db_throttle": ActionSpec(
        key="reset_demo_db_throttle",
        title="Stop the demo database overload",
        description=(
            "Clear the write-amplification flag so the demo application stops "
            "exceeding the demo table's provisioned capacity."
        ),
        risk=Risk.LOW,
        applies_to=("database_throttle",),
        env_overrides={"FAILURE_MODE": None, "WRITE_BURST": None},
    ),
}

#: Human phrasings the model tends to produce, mapped onto allowlist keys.
_ALIASES: Final[dict[str, str]] = {
    "reset_lambda": "reset_demo_lambda",
    "reset_function": "reset_demo_lambda",
    "reset_demo_function": "reset_demo_lambda",
    "redeploy_lambda": "reset_demo_lambda",
    "restart_lambda": "reset_demo_lambda",
    "reset_demo_app": "reset_demo_lambda",
    "disable_error_injection": "reset_demo_error_mode",
    "clear_error_mode": "reset_demo_error_mode",
    "reset_error_mode": "reset_demo_error_mode",
    "fix_lambda_errors": "reset_demo_error_mode",
    "reduce_latency": "reset_demo_latency_mode",
    "clear_latency": "reset_demo_latency_mode",
    "reset_latency_mode": "reset_demo_latency_mode",
    "remove_artificial_delay": "reset_demo_latency_mode",
    "restore_configuration": "restore_demo_configuration",
    "restore_config": "restore_demo_configuration",
    "fix_configuration": "restore_demo_configuration",
    "revert_configuration": "restore_demo_configuration",
    "rollback_configuration": "restore_previous_demo_version",
    "rollback_deployment": "restore_previous_demo_version",
    "restore_previous_version": "restore_previous_demo_version",
    "revert_to_previous_version": "restore_previous_demo_version",
    "rollback_lambda_version": "restore_previous_demo_version",
    "restore_previous_lambda_version": "restore_previous_demo_version",
    "reduce_write_throughput": "reset_demo_db_throttle",
    "stop_database_overload": "reset_demo_db_throttle",
    "reset_db_throttle": "reset_demo_db_throttle",
    "fix_dynamodb_throttling": "reset_demo_db_throttle",
    "increase_dynamodb_capacity": "reset_demo_db_throttle",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_action(raw: Any) -> str:
    """Reduce arbitrary model text to a canonical snake_case action label."""
    if not isinstance(raw, str):
        return ""
    slug = _NON_ALNUM.sub("_", raw.strip().lower()).strip("_")
    return slug


def resolve_action(raw: Any) -> ActionSpec | None:
    """Map model output onto an allowlisted action, or None if unmatched.

    This is the *only* function that grants execution rights. Anything it does
    not recognise is refused, which is why arbitrary strings, shell commands or
    AWS CLI fragments from the model can never run.
    """
    slug = normalise_action(raw)
    if not slug:
        return None
    if slug in ALLOWED_ACTIONS:
        return ALLOWED_ACTIONS[slug]
    if slug in _ALIASES:
        return ALLOWED_ACTIONS[_ALIASES[slug]]
    # Last resort: a unique containment match against known keys and aliases.
    hits = {
        target
        for candidate, target in (
            *((k, k) for k in ALLOWED_ACTIONS),
            *_ALIASES.items(),
        )
        if candidate in slug or slug in candidate
    }
    if len(hits) == 1:
        return ALLOWED_ACTIONS[hits.pop()]
    return None


def default_action_for(incident_type: str) -> ActionSpec:
    """Return the safe fallback action for a known failure scenario.

    Used when Bedrock is unavailable so the incident still carries an
    actionable, human-approvable recommendation.
    """
    for spec in ALLOWED_ACTIONS.values():
        if incident_type in spec.applies_to and spec.key != "restore_previous_demo_version":
            return spec
    return ALLOWED_ACTIONS["reset_demo_lambda"]


def describe_allowlist() -> list[dict[str, Any]]:
    """Render the allowlist for the API, dashboard and Bedrock prompt."""
    return [
        {
            "action": spec.key,
            "title": spec.title,
            "description": spec.description,
            "risk": spec.risk,
            "applies_to": list(spec.applies_to),
        }
        for spec in ALLOWED_ACTIONS.values()
    ]


def annotate_recommendations(
    recommendations: list[dict[str, Any]] | None,
    incident_type: str = "unknown",
) -> list[dict[str, Any]]:
    """Tag each model recommendation with its allowlist verdict.

    Recommendations survive into the UI either way - an unmatched one is shown
    to the operator as "manual remediation required" rather than discarded, so
    the model's reasoning stays visible without becoming executable.
    """
    annotated: list[dict[str, Any]] = []
    for entry in recommendations or []:
        if isinstance(entry, str):
            entry = {"action": entry}
        if not isinstance(entry, dict):
            continue
        raw_action = entry.get("action") or entry.get("name") or entry.get("operation") or ""
        spec = resolve_action(raw_action)
        applicable = spec is not None and (
            incident_type in spec.applies_to or "unknown" in spec.applies_to
        )
        annotated.append(
            {
                "proposed_action": str(raw_action)[:200],
                "action": spec.key if spec else "",
                "title": spec.title if spec else "Manual remediation required",
                "risk": spec.risk if spec else str(entry.get("risk", "UNKNOWN"))[:20].upper(),
                "reason": str(entry.get("reason") or entry.get("rationale") or "")[:600],
                "allowlisted": spec is not None,
                "applicable": applicable,
                "executable": spec is not None and applicable,
            }
        )
    return annotated
