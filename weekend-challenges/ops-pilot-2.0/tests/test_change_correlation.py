"""Change correlation scoring.

Correlation is deterministic and never claims causation. These tests pin the
ranking behaviour that the whole "what changed just before this?" story rests
on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from opspilot import change_correlator as cc


ONSET = datetime(2026, 8, 28, 10, 3, 0, tzinfo=timezone.utc)


def change(minutes_before: float, **overrides: object) -> dict:
    """Build a change record positioned relative to incident onset."""
    base = {
        "timestamp": (ONSET - timedelta(minutes=minutes_before)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service": "lambda",
        "resource": "opspilot-test-demo-app",
        "action": "UpdateFunctionConfiguration",
        "actor": "opspilot-demo-lab",
        "details": "",
        "source": "cloudtrail",
    }
    base.update(overrides)
    return base


class TestRanking:
    def test_recent_high_impact_change_is_a_likely_contributor(self) -> None:
        [ranked] = cc.rank_changes([change(1.0)], ONSET, "lambda_error")
        assert ranked["correlation"] == "likely_contributor"
        assert ranked["correlation_score"] >= 0.6

    def test_old_low_impact_change_is_unrelated(self) -> None:
        [ranked] = cc.rank_changes(
            [change(50, action="ListTags", service="s3")], ONSET, "lambda_error"
        )
        assert ranked["correlation"] == "unrelated"

    def test_unrelated_service_change_stays_unrelated(self) -> None:
        # A bucket policy edit does not explain a Lambda failure, even a recent
        # one. Scoring must not conflate "happened nearby" with "contributed".
        [ranked] = cc.rank_changes(
            [change(12, service="s3", action="PutBucketPolicy")], ONSET, "lambda_error"
        )
        assert ranked["correlation"] == "unrelated"

    def test_change_after_onset_cannot_have_contributed(self) -> None:
        [ranked] = cc.rank_changes([change(-5)], ONSET, "lambda_error")
        assert ranked["correlation"] == "after_incident"
        assert ranked["correlation_score"] == 0.0

    def test_closer_changes_outrank_older_ones(self) -> None:
        ranked = cc.rank_changes([change(12), change(1)], ONSET, "lambda_error")
        assert ranked[0]["minutes_before_incident"] == 1.0

    def test_matching_service_raises_the_score(self) -> None:
        matching = cc.rank_changes([change(1)], ONSET, "lambda_error")[0]
        unrelated = cc.rank_changes(
            [change(1, service="route53")], ONSET, "lambda_error"
        )[0]
        assert matching["correlation_score"] > unrelated["correlation_score"]

    def test_opspilot_recorded_changes_score_higher_than_inferred(self) -> None:
        logged = cc.rank_changes(
            [change(1, source="opspilot-change-log", action="fault_injection")],
            ONSET, "lambda_error",
        )[0]
        assert logged["correlation"] == "likely_contributor"
        assert any("Recorded directly by OpsPilot" in r for r in logged["correlation_reasons"])

    def test_reasons_are_always_explained(self) -> None:
        [ranked] = cc.rank_changes([change(1)], ONSET, "lambda_error")
        assert ranked["correlation_reasons"]

    def test_score_is_bounded(self) -> None:
        [ranked] = cc.rank_changes(
            [change(0.1, source="opspilot-change-log")], ONSET, "lambda_error"
        )
        assert 0.0 <= ranked["correlation_score"] <= 1.0

    def test_unparseable_timestamp_is_tolerated(self) -> None:
        [ranked] = cc.rank_changes([change(1, timestamp="not a date")], ONSET, "lambda_error")
        assert ranked["minutes_before_incident"] is None
        assert "correlation" in ranked

    def test_empty_input(self) -> None:
        assert cc.rank_changes([], ONSET, "lambda_error") == []


class TestPrimaryChange:
    def test_returns_the_top_contributor(self) -> None:
        ranked = cc.rank_changes([change(40, service="s3", action="ListTags"), change(1)],
                                 ONSET, "lambda_error")
        primary = cc.primary_change(ranked)
        assert primary is not None and primary["minutes_before_incident"] == 1.0

    def test_returns_none_when_nothing_correlates(self) -> None:
        ranked = cc.rank_changes(
            [change(55, service="s3", action="ListTags")], ONSET, "lambda_error"
        )
        assert cc.primary_change(ranked) is None


class TestSummarise:
    def test_names_the_contributing_change(self) -> None:
        ranked = cc.rank_changes([change(1)], ONSET, "lambda_error")
        summary = cc.summarise_changes(ranked)
        assert "UpdateFunctionConfiguration" in summary
        assert "1.0 minutes before onset" in summary

    def test_reports_possible_contributors(self) -> None:
        # High-impact action, but on a different service and 12 minutes out:
        # enough to be worth showing, not enough to call a contributor.
        ranked = cc.rank_changes(
            [change(12, service="cloudformation", action="UpdateStack")],
            ONSET, "lambda_error",
        )
        assert ranked[0]["correlation"] == "possible_contributor"
        assert "possible" in cc.summarise_changes(ranked).lower()

    def test_reports_nothing_found_honestly(self) -> None:
        assert "No contributing" in cc.summarise_changes([])


class TestRestorativeChanges:
    """A change that restores health cannot be the cause of a new failure.

    Without this rule OpsPilot's own reset - applied moments before a fault is
    injected - outranks the fault itself, because it is recent, high blast
    radius and touches the failing service.
    """

    @pytest.mark.parametrize(
        "action", ["remediation", "configuration_reset", "restore", "rollback"]
    )
    def test_restorative_actions_never_reach_contributor(self, action: str) -> None:
        [ranked] = cc.rank_changes(
            [change(0.5, action=action, source="opspilot-change-log")],
            ONSET, "lambda_error",
        )
        assert ranked["correlation"] != "likely_contributor"
        assert ranked["correlation_score"] <= 0.30
        assert any("Restorative" in r for r in ranked["correlation_reasons"])

    def test_the_fault_outranks_the_reset_that_preceded_it(self) -> None:
        ranked = cc.rank_changes(
            [
                change(1.5, action="configuration_reset", source="opspilot-change-log"),
                change(1.0, action="fault_injection", source="opspilot-change-log"),
            ],
            ONSET, "lambda_error",
        )
        assert ranked[0]["action"] == "fault_injection"
        assert ranked[0]["correlation"] == "likely_contributor"
        assert cc.primary_change(ranked)["action"] == "fault_injection"
