"""Prompt construction and validation of model output.

Validation is what stops a malformed or hallucinated response reaching the
incident record. Anything it cannot vouch for becomes an honest fallback.
"""

from __future__ import annotations

import pytest

from opspilot import models, prompts


class TestValidateAnalysis:
    def _good(self) -> dict:
        return {
            "summary": "DynamoDB throttling caused elevated API errors.",
            "severity": "HIGH",
            "root_cause": {
                "description": "A recent deployment increased request volume.",
                "confidence": 0.91,
                "category": "deployment",
            },
            "timeline": [{"timestamp": "2026-08-28T10:01:00Z", "event": "Version deployed"}],
            "evidence": ["Invocation rate rose 43%"],
            "contributing_factors": ["No deployment validation alarm"],
            "recommended_actions": [
                {"action": "restore_previous_demo_version", "risk": "LOW", "reason": "healthy"}
            ],
        }

    def test_well_formed_response(self) -> None:
        result = prompts.validate_analysis(self._good())
        assert result is not None
        assert result["severity"] == "HIGH"
        assert result["root_cause"]["confidence"] == 0.91

    @pytest.mark.parametrize("value", [None, "string", 42, [], True])
    def test_non_dict_rejected(self, value: object) -> None:
        assert prompts.validate_analysis(value) is None

    def test_empty_response_rejected(self) -> None:
        assert prompts.validate_analysis({}) is None

    def test_summary_only_is_accepted(self) -> None:
        result = prompts.validate_analysis({"summary": "something broke"})
        assert result is not None and result["root_cause"]["confidence"] == 0.0

    def test_root_cause_as_bare_string(self) -> None:
        result = prompts.validate_analysis({"root_cause": "the config was wrong"})
        assert result is not None
        assert result["root_cause"]["description"] == "the config was wrong"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.91, 0.91), (91, 0.91), ("0.91", 0.91), ("91%", 0.91), (100, 1.0),
            # Out-of-contract values resolve downwards: understating confidence
            # in an automated diagnosis is the safe direction to fail.
            (1.5, 0.015), (150, 1.0), (-3, 0.0),
            ("nonsense", 0.0), (None, 0.0), ([], 0.0), (True, 0.0),
        ],
    )
    def test_confidence_is_clamped_and_coerced(self, raw: object, expected: float) -> None:
        result = prompts.validate_analysis(
            {"summary": "x", "root_cause": {"description": "y", "confidence": raw}}
        )
        assert result["root_cause"]["confidence"] == pytest.approx(expected, abs=0.02)

    @pytest.mark.parametrize("raw", [1.0, 1.5, 91, 100, 150, 99999, "100%", "300%"])
    def test_confidence_never_exceeds_one(self, raw: object) -> None:
        result = prompts.validate_analysis(
            {"summary": "x", "root_cause": {"description": "y", "confidence": raw}}
        )
        assert 0.0 <= result["root_cause"]["confidence"] <= 1.0

    def test_unknown_severity_falls_back(self) -> None:
        result = prompts.validate_analysis({"summary": "x", "severity": "APOCALYPTIC"})
        assert result["severity"] == models.Severity.MEDIUM

    def test_string_lists_are_coerced(self) -> None:
        result = prompts.validate_analysis({"summary": "x", "evidence": "one fact"})
        assert result["evidence"] == ["one fact"]

    def test_lists_are_bounded(self) -> None:
        result = prompts.validate_analysis(
            {"summary": "x", "evidence": [f"fact {i}" for i in range(200)]}
        )
        assert len(result["evidence"]) == 20

    def test_timeline_entries_without_events_are_dropped(self) -> None:
        result = prompts.validate_analysis(
            {"summary": "x", "timeline": [{"timestamp": "2026-08-28T10:00:00Z"}, "bare string"]}
        )
        assert len(result["timeline"]) == 1

    def test_actions_coerced_from_strings(self) -> None:
        result = prompts.validate_analysis(
            {"summary": "x", "recommended_actions": "reset_demo_lambda"}
        )
        assert result["recommended_actions"][0]["action"] == "reset_demo_lambda"

    def test_deeply_malformed_response_never_raises(self) -> None:
        chaos = {
            "summary": {"nested": ["weird"]},
            "severity": [1, 2],
            "root_cause": ["not", "a", "dict"],
            "timeline": "not a list",
            "evidence": {"not": "a list"},
            "recommended_actions": 42,
        }
        assert prompts.validate_analysis(chaos) is not None


class TestFallbackAnalysis:
    def test_fallback_never_invents_a_diagnosis(self) -> None:
        fallback = prompts.fallback_analysis("Bedrock unavailable")
        assert fallback["root_cause"]["confidence"] == 0
        assert fallback["severity"] == models.Severity.UNKNOWN
        assert fallback["evidence"] == []
        assert fallback["recommended_actions"] == []
        assert "unavailable" in fallback["summary"].lower()


class TestPromptConstruction:
    def _incident(self) -> dict:
        return {
            "incident_id": "INC-1",
            "title": "Demo Lambda is failing",
            "detected_at": "2026-08-28T10:00:00Z",
            "alarm_name": "opspilot-test-demo-lambda-errors",
            "affected_service": "opspilot-test-demo-app",
            "incident_type": "lambda_error",
        }

    def test_prompt_contains_evidence_and_schema(self) -> None:
        bundle = {
            "alarm": {"state": "ALARM"},
            "changes": [{"action": "UpdateFunctionConfiguration"}],
            "metrics": [{"metric": "Errors"}],
            "logs": [{"message": "boom"}],
            "similar_incidents": [{"incident_id": "INC-0"}],
            "sources": {"cloudtrail": {"available": False}},
        }
        prompt = prompts.build_root_cause_prompt(self._incident(), bundle)
        assert "## INCIDENT" in prompt
        assert "## ALLOWED REMEDIATION ACTIONS" in prompt
        assert "recommended_actions" in prompt
        assert "reset_demo_lambda" in prompt

    def test_history_is_labelled_as_context_not_proof(self) -> None:
        bundle = {"similar_incidents": [{"incident_id": "INC-0"}]}
        prompt = prompts.build_root_cause_prompt(self._incident(), bundle)
        assert "do not assume the current incident has the same root cause" in prompt

    def test_prompt_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Evidence collection is bounded for cost and for small-model
        # compatibility; a huge log dump must not blow the budget.
        monkeypatch.setattr(prompts.config, "MAX_PROMPT_CHARS", 2000)
        bundle = {"logs": [{"message": "x" * 500} for _ in range(500)]}
        prompt = prompts.build_root_cause_prompt(self._incident(), bundle)
        assert "truncated" in prompt
        assert len(prompt) < 20000

    def test_empty_bundle_still_produces_a_prompt(self) -> None:
        prompt = prompts.build_root_cause_prompt(self._incident(), {})
        assert "## REQUIRED OUTPUT" in prompt

    def test_system_prompt_forbids_invention(self) -> None:
        assert "Do not invent" in prompts.ROOT_CAUSE_SYSTEM_PROMPT
        assert "Return valid JSON only" in prompts.ROOT_CAUSE_SYSTEM_PROMPT
        assert "Never claim an action has been performed" in prompts.ROOT_CAUSE_SYSTEM_PROMPT


class TestPostmortemValidation:
    def test_valid_narrative(self) -> None:
        result = prompts.validate_postmortem({
            "executive_summary": "The service failed and recovered.",
            "impact": "Users saw errors.",
            "what_went_well": ["Detection was fast"],
            "what_went_wrong": ["No validation alarm"],
            "preventive_actions": ["Add an alarm"],
            "lessons_learned": ["Correlate changes"],
        })
        assert result is not None and result["what_went_well"] == ["Detection was fast"]

    @pytest.mark.parametrize("value", [None, {}, {"impact": "x"}, "string", 42])
    def test_missing_summary_rejected(self, value: object) -> None:
        assert prompts.validate_postmortem(value) is None

    def test_prompt_is_built_from_facts(self) -> None:
        prompt = prompts.build_postmortem_prompt({"incident_id": "INC-1", "severity": "HIGH"})
        assert "INC-1" in prompt and "executive_summary" in prompt
