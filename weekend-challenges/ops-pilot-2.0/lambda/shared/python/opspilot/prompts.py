"""Bedrock prompt construction and response validation.

Prompts are written for *basic* text-generation models: short instructions,
an explicit JSON schema, no tool use, no chain-of-thought requirements. The
evidence block is truncated to ``MAX_PROMPT_CHARS`` so a small model is never
handed more context than it can use.
"""

from __future__ import annotations

import json
from typing import Any

from . import config, models, remediation_actions
from .logging_utils import get_logger

log = get_logger("prompts")

ROOT_CAUSE_SYSTEM_PROMPT = """You are an AWS Site Reliability Engineer performing incident analysis.

Rules you must follow:
- Analyse only the evidence supplied. Do not invent AWS events, metrics, log lines or resource names.
- Only make claims supported by the supplied data.
- Clearly separate observed facts from hypotheses. Facts belong in "evidence"; reasoning belongs in "root_cause".
- Identify the single most likely root cause.
- Assign a confidence score between 0 and 1 that honestly reflects the strength of the evidence.
- Identify contributing factors that made the incident possible or worse.
- Recommend the safest remediation from the allowed actions list you are given.
- Never recommend a destructive action unless there is no safe alternative.
- Never claim an action has been performed or has succeeded. You only analyse and recommend.
- Previous incidents may provide useful context, but do not assume the current incident has the same root cause.
- Return valid JSON only. No prose, no Markdown, no code fences."""

_SCHEMA_BLOCK = """{
  "summary": "one sentence describing what happened",
  "severity": "CRITICAL | HIGH | MEDIUM | LOW",
  "root_cause": {
    "description": "what caused the incident, grounded in the evidence",
    "confidence": 0.0,
    "category": "deployment | configuration | capacity | code_defect | dependency | unknown"
  },
  "timeline": [
    {"timestamp": "ISO-8601", "event": "what happened at this moment"}
  ],
  "evidence": [
    "an observed fact taken directly from the supplied data"
  ],
  "contributing_factors": [
    "a condition that made the incident possible or worse"
  ],
  "recommended_actions": [
    {"action": "one of the allowed action keys", "risk": "LOW | MEDIUM | HIGH", "reason": "why this is the safest fix"}
  ]
}"""


def build_root_cause_prompt(incident: dict[str, Any], bundle: dict[str, Any]) -> str:
    """Assemble the root-cause analysis prompt from collected evidence."""
    allowed = "\n".join(
        f"- {spec['action']}: {spec['description']} (risk: {spec['risk']})"
        for spec in remediation_actions.describe_allowlist()
    )

    sections: list[str] = [
        "## INCIDENT",
        json.dumps(
            {
                "incident_id": incident.get("incident_id"),
                "title": incident.get("title"),
                "detected_at": incident.get("detected_at"),
                "alarm_name": incident.get("alarm_name"),
                "alarm_reason": incident.get("alarm_reason"),
                "affected_service": incident.get("affected_service"),
                "incident_type": incident.get("incident_type"),
            },
            indent=2,
            default=str,
        ),
    ]

    sections += _evidence_sections(bundle)

    return "\n\n".join(
        [
            *sections,
            "## ALLOWED REMEDIATION ACTIONS",
            "You may only recommend actions from this list. Use the exact key.",
            allowed,
            "## REQUIRED OUTPUT",
            "Return a single JSON object with exactly this shape:",
            _SCHEMA_BLOCK,
            "Return only the JSON object.",
        ]
    )


def _evidence_sections(bundle: dict[str, Any]) -> list[str]:
    """Render each evidence source, respecting the total prompt budget.

    Sources are emitted in priority order and the budget is consumed as we go,
    so the highest-signal evidence survives truncation on a small model.
    """
    budget = config.MAX_PROMPT_CHARS
    sections: list[str] = []

    ordered: list[tuple[str, Any]] = [
        ("ALARM", bundle.get("alarm")),
        ("INFRASTRUCTURE CHANGES BEFORE THE INCIDENT", bundle.get("changes")),
        ("METRICS", bundle.get("metrics")),
        ("APPLICATION STATE", bundle.get("application_state")),
        ("RECENT LOG EVENTS", bundle.get("logs")),
        ("PREVIOUS SIMILAR INCIDENTS", bundle.get("similar_incidents")),
        ("EVIDENCE SOURCE AVAILABILITY", bundle.get("sources")),
    ]

    for heading, payload in ordered:
        if payload in (None, [], {}):
            continue
        rendered = json.dumps(payload, indent=2, default=str)
        if len(rendered) > budget:
            rendered = rendered[: max(budget, 0)] + "\n... [truncated to fit prompt budget]"
        if budget <= 0:
            sections.append(f"## {heading}\n[omitted: prompt budget exhausted]")
            continue
        budget -= len(rendered)
        note = ""
        if heading.startswith("PREVIOUS SIMILAR"):
            note = (
                "\nThese are historical incidents with a similar signature. They may "
                "provide useful context, but do not assume the current incident has "
                "the same root cause.\n"
            )
        sections.append(f"## {heading}{note}\n{rendered}")

    return sections


# --- Validation ---------------------------------------------------------------
def fallback_analysis(reason: str) -> dict[str, Any]:
    """The honest response when Bedrock cannot produce an analysis.

    OpsPilot never fabricates a diagnosis: confidence is zero and the summary
    says plainly that automated analysis was unavailable.
    """
    return {
        "summary": "Automated AI analysis was unavailable.",
        "severity": models.Severity.UNKNOWN,
        "root_cause": {
            "description": "Insufficient evidence for automated diagnosis.",
            "confidence": 0,
            "category": "unknown",
        },
        "timeline": [],
        "evidence": [],
        "contributing_factors": [],
        "recommended_actions": [],
        "_fallback_reason": reason[:300],
    }


def validate_analysis(parsed: Any) -> dict[str, Any] | None:
    """Coerce and validate a parsed model response into OpsPilot's shape.

    Missing optional fields are filled with safe defaults; a response with no
    usable summary *and* no root cause is rejected so the caller falls back
    rather than storing an empty diagnosis.
    """
    if not isinstance(parsed, dict):
        return None

    root_cause_raw = parsed.get("root_cause")
    if isinstance(root_cause_raw, str):
        root_cause_raw = {"description": root_cause_raw}
    if not isinstance(root_cause_raw, dict):
        root_cause_raw = {}

    description = _as_text(root_cause_raw.get("description") or parsed.get("cause"), 2000)
    summary = _as_text(parsed.get("summary"), 1000)
    if not summary and not description:
        log.warning("analysis_validation_failed", reason="no summary or root cause")
        return None

    confidence = _as_confidence(root_cause_raw.get("confidence", parsed.get("confidence")))

    validated = {
        "summary": summary or description[:300],
        "severity": models.Severity.normalise(parsed.get("severity")),
        "root_cause": {
            "description": description or summary,
            "confidence": confidence,
            "category": _as_text(root_cause_raw.get("category"), 60).lower() or "unknown",
        },
        "timeline": _as_timeline(parsed.get("timeline")),
        "evidence": _as_string_list(parsed.get("evidence"), 20, 500),
        "contributing_factors": _as_string_list(parsed.get("contributing_factors"), 10, 400),
        "recommended_actions": _as_actions(parsed.get("recommended_actions")),
    }
    return validated


def _as_text(value: Any, limit: int) -> str:
    """Coerce a model value into bounded plain text."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    return str(value).strip()[:limit]


def _as_confidence(value: Any) -> float:
    """Coerce a model confidence into [0, 1].

    Models express confidence inconsistently: ``0.91``, ``91``, ``"91%"``. The
    rule applied here is that anything above 1 was meant as a percentage, so it
    is divided by 100 and then clamped.

    A value like ``1.5`` is outside the contract either way. Dividing it yields
    ``0.015`` rather than clamping up to ``1.0``, which is deliberate: for an
    operations tool, *understating* confidence in an automated diagnosis is the
    safe direction to fail. Confidence can never exceed 1.0.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%").strip())
        except ValueError:
            return 0.0
    if not isinstance(value, (int, float)):
        return 0.0

    number = float(value)
    if number > 1.0:
        number = number / 100.0
    return round(max(0.0, min(1.0, number)), 3)


def _as_string_list(value: Any, max_items: int, max_len: int) -> list[str]:
    """Coerce a model value into a bounded list of strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:max_items]:
        text = _as_text(item, max_len)
        if text:
            out.append(text)
    return out


def _as_timeline(value: Any) -> list[dict[str, str]]:
    """Coerce model timeline output into bounded {timestamp, event} entries."""
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value[:30]:
        if isinstance(item, str):
            entries.append({"timestamp": "", "event": item[:400]})
            continue
        if not isinstance(item, dict):
            continue
        event = _as_text(item.get("event") or item.get("description"), 400)
        if not event:
            continue
        stamp = item.get("timestamp") or item.get("time") or ""
        parsed = models.parse_iso(stamp)
        entries.append({"timestamp": models.iso(parsed) if parsed else "", "event": event})
    return entries


def _as_actions(value: Any) -> list[dict[str, str]]:
    """Coerce model recommendations into bounded action dicts."""
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []
    actions: list[dict[str, str]] = []
    for item in value[:6]:
        if isinstance(item, str):
            actions.append({"action": item[:200], "risk": "UNKNOWN", "reason": ""})
            continue
        if not isinstance(item, dict):
            continue
        action = _as_text(item.get("action") or item.get("name") or item.get("operation"), 200)
        if not action:
            continue
        actions.append(
            {
                "action": action,
                "risk": _as_text(item.get("risk"), 20).upper() or "UNKNOWN",
                "reason": _as_text(item.get("reason") or item.get("rationale"), 600),
            }
        )
    return actions


# --- Postmortem narrative -----------------------------------------------------
POSTMORTEM_SYSTEM_PROMPT = """You are an AWS Site Reliability Engineer writing the narrative sections of a blameless postmortem.

Rules you must follow:
- Use only the incident facts supplied. Do not invent events, metrics or resource names.
- Be blameless: describe systems and processes, never individuals.
- Be concise and concrete. Two to four sentences per section, or three to five bullet strings for list sections.
- Do not restate the raw data; interpret it.
- Return valid JSON only. No prose, no Markdown, no code fences."""

_POSTMORTEM_SCHEMA = """{
  "executive_summary": "2-4 sentences a non-engineer can follow",
  "impact": "what the failure meant for users of the service",
  "what_went_well": ["..."],
  "what_went_wrong": ["..."],
  "preventive_actions": ["..."],
  "lessons_learned": ["..."]
}"""


def build_postmortem_prompt(facts: dict[str, Any]) -> str:
    """Build the prompt for the narrative half of the postmortem.

    Only prose is model-generated; every fact, timestamp and metric in the
    published document comes from the stored incident record.
    """
    rendered = json.dumps(facts, indent=2, default=str)
    if len(rendered) > config.MAX_PROMPT_CHARS:
        rendered = rendered[: config.MAX_PROMPT_CHARS] + "\n... [truncated]"
    return "\n\n".join(
        [
            "## INCIDENT FACTS",
            rendered,
            "## REQUIRED OUTPUT",
            "Return a single JSON object with exactly this shape:",
            _POSTMORTEM_SCHEMA,
            "Return only the JSON object.",
        ]
    )


def validate_postmortem(parsed: Any) -> dict[str, Any] | None:
    """Validate the narrative sections returned for a postmortem."""
    if not isinstance(parsed, dict):
        return None
    summary = _as_text(parsed.get("executive_summary"), 2000)
    if not summary:
        return None
    return {
        "executive_summary": summary,
        "impact": _as_text(parsed.get("impact"), 1500),
        "what_went_well": _as_string_list(parsed.get("what_went_well"), 6, 400),
        "what_went_wrong": _as_string_list(parsed.get("what_went_wrong"), 6, 400),
        "preventive_actions": _as_string_list(parsed.get("preventive_actions"), 6, 400),
        "lessons_learned": _as_string_list(parsed.get("lessons_learned"), 6, 400),
    }
