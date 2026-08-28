"""Incident model, state machine and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from opspilot import models


class TestIdempotency:
    def test_same_alarm_and_time_yields_same_key(self) -> None:
        a = models.dedupe_key("demo-errors", "2026-08-28T10:00:00Z")
        b = models.dedupe_key("demo-errors", "2026-08-28T10:00:00Z")
        assert a == b

    def test_nearby_times_collapse_into_one_key(self) -> None:
        # A flapping alarm inside one 5-minute bucket is one incident.
        a = models.dedupe_key("demo-errors", "2026-08-28T10:00:10Z")
        b = models.dedupe_key("demo-errors", "2026-08-28T10:02:30Z")
        assert a == b

    def test_distant_times_yield_different_keys(self) -> None:
        a = models.dedupe_key("demo-errors", "2026-08-28T10:00:00Z")
        b = models.dedupe_key("demo-errors", "2026-08-28T10:30:00Z")
        assert a != b

    def test_different_alarms_yield_different_keys(self) -> None:
        a = models.dedupe_key("demo-errors", "2026-08-28T10:00:00Z")
        b = models.dedupe_key("demo-latency", "2026-08-28T10:00:00Z")
        assert a != b

    def test_seeded_incident_id_is_deterministic(self) -> None:
        # This is what makes the DynamoDB conditional write a real idempotency
        # guard: a replayed event resolves to the same primary key.
        when = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        key = models.dedupe_key("demo-errors", "2026-08-28T10:00:00Z")
        assert models.new_incident_id(when, seed=key) == models.new_incident_id(when, seed=key)

    def test_unseeded_incident_ids_are_unique(self) -> None:
        when = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        assert models.new_incident_id(when) != models.new_incident_id(when)

    def test_incident_id_is_readable_and_sortable(self) -> None:
        when = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        assert models.new_incident_id(when, seed="x").startswith("INC-20260828-")


class TestStateMachine:
    def test_happy_path_is_legal(self) -> None:
        path = [
            models.IncidentStatus.DETECTED,
            models.IncidentStatus.INVESTIGATING,
            models.IncidentStatus.AWAITING_APPROVAL,
            models.IncidentStatus.REMEDIATING,
            models.IncidentStatus.VERIFYING,
            models.IncidentStatus.RESOLVED,
        ]
        for current, target in zip(path, path[1:], strict=False):
            assert models.can_transition(current, target), f"{current} -> {target}"

    def test_cannot_skip_approval(self) -> None:
        # The human gate cannot be bypassed by a state transition.
        assert not models.can_transition(
            models.IncidentStatus.INVESTIGATING, models.IncidentStatus.REMEDIATING
        )

    def test_cannot_resolve_straight_from_detected(self) -> None:
        assert not models.can_transition(
            models.IncidentStatus.DETECTED, models.IncidentStatus.RESOLVED
        )

    def test_self_transition_allowed(self) -> None:
        assert models.can_transition(
            models.IncidentStatus.INVESTIGATING, models.IncidentStatus.INVESTIGATING
        )

    def test_terminal_states_can_be_reopened_for_investigation(self) -> None:
        assert models.can_transition(
            models.IncidentStatus.RESOLVED, models.IncidentStatus.INVESTIGATING
        )

    def test_every_status_has_a_transition_entry(self) -> None:
        assert set(models.TRANSITIONS) == set(models.IncidentStatus.ALL)


class TestSeverity:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("HIGH", "HIGH"), ("high", "HIGH"), ("  Critical ", "CRITICAL"),
            ("SEV1", "CRITICAL"), ("P2", "HIGH"), ("major", "HIGH"),
            ("minor", "LOW"), ("warning", "MEDIUM"),
        ],
    )
    def test_normalises_known_values(self, raw: str, expected: str) -> None:
        assert models.Severity.normalise(raw) == expected

    @pytest.mark.parametrize("raw", [None, 42, "", "gibberish", [], {}])
    def test_falls_back_for_unknown_values(self, raw: object) -> None:
        assert models.Severity.normalise(raw) == models.Severity.MEDIUM

    def test_explicit_default_is_honoured(self) -> None:
        assert models.Severity.normalise("nonsense", "LOW") == "LOW"


class TestTimeHelpers:
    @pytest.mark.parametrize(
        "raw",
        ["2026-08-28T10:00:00Z", "2026-08-28T10:00:00+00:00", "2026-08-28T10:00:00.123456Z"],
    )
    def test_parses_aws_timestamp_formats(self, raw: str) -> None:
        parsed = models.parse_iso(raw)
        assert parsed is not None and parsed.year == 2026

    @pytest.mark.parametrize("raw", [None, "", "  ", "not a date", 12345, []])
    def test_unparseable_returns_none(self, raw: object) -> None:
        assert models.parse_iso(raw) is None

    def test_datetime_passthrough_gets_utc(self) -> None:
        naive = datetime(2026, 8, 28, 10, 0)
        assert models.parse_iso(naive).tzinfo is not None

    def test_iso_round_trip(self) -> None:
        moment = datetime(2026, 8, 28, 10, 1, 42, tzinfo=timezone.utc)
        assert models.iso(moment) == "2026-08-28T10:01:42Z"

    def test_minutes_between(self) -> None:
        assert models.minutes_between("2026-08-28T10:00:00Z", "2026-08-28T10:07:30Z") == 7.5

    def test_minutes_between_unparseable_is_none(self) -> None:
        assert models.minutes_between("nonsense", "2026-08-28T10:00:00Z") is None


class TestSignature:
    def test_same_failure_shape_matches(self) -> None:
        a = models.signature("opspilot-demo-app", "lambda_error")
        b = models.signature("OpsPilot-Demo-App", "LAMBDA_ERROR")
        assert a == b

    def test_different_failure_shapes_differ(self) -> None:
        assert models.signature("svc", "lambda_error") != models.signature("svc", "lambda_latency")

    def test_missing_values_are_tolerated(self) -> None:
        assert models.signature("", "") == "unknown|unknown"


class TestTimeline:
    def test_merge_sorts_chronologically(self) -> None:
        merged = models.merge_timeline(
            [models.timeline_entry("2026-08-28T10:05:00Z", "later")],
            [models.timeline_entry("2026-08-28T10:01:00Z", "earlier")],
        )
        assert [e["event"] for e in merged] == ["earlier", "later"]

    def test_merge_drops_duplicates(self) -> None:
        entry = models.timeline_entry("2026-08-28T10:01:00Z", "same")
        assert len(models.merge_timeline([entry], [dict(entry)])) == 1

    def test_merge_tolerates_none_and_junk(self) -> None:
        assert models.merge_timeline(None, None) == []
        assert models.merge_timeline([{"bad": 1}, "junk"], None) == [{"bad": 1}]

    def test_entries_carry_an_icon(self) -> None:
        entry = models.timeline_entry("2026-08-28T10:00:00Z", "x", models.TimelineKind.CHANGE)
        assert entry["icon"] and entry["kind"] == "change"


class TestBuildIncident:
    def _seed(self) -> models.IncidentSeed:
        return models.IncidentSeed(
            alarm_name="opspilot-test-demo-lambda-errors",
            affected_service="opspilot-test-demo-app",
            incident_type="lambda_error",
            severity="HIGH",
            title="Demo Lambda is failing",
            description="errors",
            detected_at="2026-08-28T10:00:00Z",
            alarm_reason="Threshold crossed",
        )

    def test_new_incident_shape(self) -> None:
        incident = models.build_incident(self._seed(), "INC-1", "dedupe")
        assert incident["status"] == models.IncidentStatus.DETECTED
        assert incident["remediation_status"] == models.RemediationStatus.NOT_STARTED
        assert incident["verification_status"] == models.VerificationStatus.PENDING
        assert incident["ai_status"] == models.AIStatus.PENDING
        assert incident["confidence"] == 0
        assert incident["signature"] == "opspilot-test-demo-app|lambda_error"

    def test_timeline_seeded_with_alarm_and_open(self) -> None:
        incident = models.build_incident(self._seed(), "INC-1", "dedupe")
        assert len(incident["timeline"]) == 2

    def test_ttl_is_in_the_future(self) -> None:
        incident = models.build_incident(self._seed(), "INC-1", "dedupe")
        assert incident["ttl"] > (models.utcnow() + timedelta(days=80)).timestamp()

    def test_every_documented_field_is_present(self) -> None:
        incident = models.build_incident(self._seed(), "INC-1", "dedupe")
        required = {
            "incident_id", "status", "severity", "title", "description",
            "detected_at", "updated_at", "resolved_at", "source", "alarm_name",
            "affected_service", "root_cause", "confidence", "evidence",
            "timeline", "changes", "recommendations", "approved_action",
            "remediation_status", "verification_status", "postmortem_location",
            "similar_incidents",
        }
        assert required <= set(incident)
