"""Resilient parsing of model output.

Small text-generation models routinely wrap JSON in Markdown fences, add prose
around it, or emit trailing commas. None of that may break the incident
workflow, so every defect below is handled rather than raised.
"""

from __future__ import annotations

import pytest

from opspilot import bedrock


class TestStripCodeFences:
    def test_plain_json_untouched(self) -> None:
        assert bedrock.strip_code_fences('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self) -> None:
        assert bedrock.strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_fence(self) -> None:
        assert bedrock.strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_fence_with_surrounding_prose(self) -> None:
        text = 'Here is the analysis:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert bedrock.strip_code_fences(text) == '{"a": 1}'


class TestExtractJsonObject:
    def test_extracts_object_from_prose(self) -> None:
        assert bedrock.extract_json_object('blah {"a": 1} trailing') == '{"a": 1}'

    def test_handles_nested_objects(self) -> None:
        text = 'x {"a": {"b": {"c": 1}}} y'
        assert bedrock.extract_json_object(text) == '{"a": {"b": {"c": 1}}}'

    def test_ignores_braces_inside_strings(self) -> None:
        text = '{"msg": "a } brace", "n": 1}'
        assert bedrock.extract_json_object(text) == text

    def test_ignores_escaped_quotes(self) -> None:
        text = r'{"msg": "he said \" } \"", "n": 1}'
        assert bedrock.extract_json_object(text) == text

    def test_returns_none_without_object(self) -> None:
        assert bedrock.extract_json_object("no json here") is None


class TestParseJsonResponse:
    def test_clean_json(self) -> None:
        assert bedrock.parse_json_response('{"summary": "x"}') == {"summary": "x"}

    def test_fenced_json(self) -> None:
        # This is what amazon.nova-lite-v1:0 actually returns in practice.
        raw = '```json\n{\n    "ok": true\n}\n```'
        assert bedrock.parse_json_response(raw) == {"ok": True}

    def test_prose_wrapped_json(self) -> None:
        raw = 'Based on the evidence:\n{"summary": "throttling"}\nThat is my analysis.'
        assert bedrock.parse_json_response(raw) == {"summary": "throttling"}

    def test_trailing_commas_repaired(self) -> None:
        raw = '{"a": 1, "b": [1, 2,],}'
        assert bedrock.parse_json_response(raw) == {"a": 1, "b": [1, 2]}

    def test_python_literals_repaired(self) -> None:
        raw = '{"a": True, "b": False, "c": None}'
        assert bedrock.parse_json_response(raw) == {"a": True, "b": False, "c": None}

    def test_list_response_takes_first_object(self) -> None:
        assert bedrock.parse_json_response('[{"a": 1}, {"b": 2}]') == {"a": 1}

    @pytest.mark.parametrize("raw", ["", "   ", "not json at all", "```\n\n```", "[1, 2, 3]"])
    def test_unusable_output_returns_none(self, raw: str) -> None:
        # None is the contract: the caller then falls back honestly rather than
        # inventing a diagnosis.
        assert bedrock.parse_json_response(raw) is None
