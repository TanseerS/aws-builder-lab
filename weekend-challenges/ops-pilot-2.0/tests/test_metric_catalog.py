"""Rehydrating the compact metric catalog.

Terraform ships this catalog in a shortened form because a Lambda's whole
environment must fit in 4 KB. Getting the expansion wrong would silently
collect the wrong metrics, so it is pinned here.
"""

from __future__ import annotations

import json

import pytest

from opspilot import evidence

COMPACT = {
    "p": {
        "lambda_errors": {"ns": "AWS/Lambda", "m": "Errors", "s": "Sum", "d": "fn", "e": True},
        "app_errors": {"ns": "OpsPilot/DemoApp", "m": "HttpErrors", "s": "Sum", "d": "svc", "e": True},
        "db_throttles": {"ns": "AWS/DynamoDB", "m": "WriteThrottleEvents", "s": "Sum", "d": "tbl", "e": True},
        "lambda_duration": {"ns": "AWS/Lambda", "m": "Duration", "s": "Average", "d": "fn", "e": False},
    },
    "c": {
        "default": ["lambda_errors", "lambda_duration"],
        "lambda_error": ["lambda_errors", "app_errors"],
        "database_throttle": ["db_throttles"],
    },
}


class TestLoadMetricCatalog:
    def test_expands_scenarios(self) -> None:
        catalog = evidence.load_metric_catalog(json.dumps(COMPACT))
        assert set(catalog) == {"default", "lambda_error", "database_throttle"}
        assert len(catalog["lambda_error"]) == 2

    def test_resolves_dimension_sets(self) -> None:
        catalog = evidence.load_metric_catalog(json.dumps(COMPACT))
        by_metric = {p["metric_name"]: p for p in catalog["lambda_error"]}
        assert by_metric["Errors"]["dimensions"] == {"FunctionName": "opspilot-test-demo-app"}
        assert by_metric["HttpErrors"]["dimensions"] == {"Service": "opspilot-test-demo-app"}

        throttle = catalog["database_throttle"][0]
        assert throttle["dimensions"] == {"TableName": "opspilot-test-demo-table"}

    def test_preserves_statistic_and_error_signal(self) -> None:
        catalog = evidence.load_metric_catalog(json.dumps(COMPACT))
        by_metric = {p["metric_name"]: p for p in catalog["default"]}
        assert by_metric["Duration"]["statistic"] == "Average"
        assert by_metric["Duration"]["error_signal"] is False
        assert by_metric["Errors"]["error_signal"] is True

    def test_probes_carry_everything_get_metric_series_needs(self) -> None:
        catalog = evidence.load_metric_catalog(json.dumps(COMPACT))
        required = {"namespace", "metric_name", "statistic", "dimensions", "period", "error_signal"}
        for probes in catalog.values():
            for probe in probes:
                assert required <= set(probe)

    def test_unknown_probe_reference_is_skipped(self) -> None:
        payload = {"p": COMPACT["p"], "c": {"x": ["lambda_errors", "does_not_exist"]}}
        assert len(evidence.load_metric_catalog(json.dumps(payload))["x"]) == 1

    @pytest.mark.parametrize(
        "raw", ["", "not json", "[]", "null", '{"p": "bad"}', '{"c": 42}', "{}"]
    )
    def test_malformed_input_yields_an_empty_catalog(self, raw: str) -> None:
        # Never raises: metrics are then reported unavailable, which is honest.
        assert evidence.load_metric_catalog(raw) == {}

    def test_accepts_an_already_parsed_dict(self) -> None:
        assert evidence.load_metric_catalog(COMPACT)["default"]
