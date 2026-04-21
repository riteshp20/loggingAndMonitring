"""
Unit tests for Phase 4 Part 1 — AlertRouter severity routing.

No external services required.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from alert_router.models import Channel, RoutingDecision
from alert_router.router import AlertRouter
from alert_router.rules import SEVERITY_RULES, get_rule
from report.models import IncidentReport


# ── helpers ───────────────────────────────────────────────────────────────────

_ONCALL_P1 = {
    "team": "payments",
    "oncall": "payments-oncall@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD11111",
    "escalation_policy": "payments-escalation",
    "tier": "P1",
}

_ONCALL_P3 = {
    "team": "personalization",
    "oncall": "personalization-oncall@company.com",
    "slack_channel": "#personalization-oncall",
    "pagerduty_service_id": "PD55555",
    "escalation_policy": "personalization-escalation",
    "tier": "P3",
}


def _make_report(**overrides) -> IncidentReport:
    defaults = dict(
        report_id=str(uuid.uuid4()),
        event_id=str(uuid.uuid4()),
        service="payment-svc",
        severity="P1",
        anomaly_type="zscore_critical",
        timestamp="2026-04-21T12:00:00+00:00",
        generated_at="2026-04-21T12:01:00+00:00",
        summary="DB pool exhausted. Checkout failing.",
        affected_services=["payment-svc"],
        probable_cause="Pool exhaustion.",
        evidence=["error_rate=0.08"],
        suggested_actions=["Roll back."],
        estimated_user_impact="All checkouts failing.",
        confidence_score=0.88,
        ai_generated=True,
        oncall_owner=_ONCALL_P1,
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=0,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


@pytest.fixture
def router() -> AlertRouter:
    return AlertRouter()


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingDecision model
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingDecisionModel:
    def test_is_routed_true_by_default(self):
        d = RoutingDecision(
            report_id="r1", service="svc", severity="P1",
            channels=[Channel.SLACK], slack_targets=["#incidents"],
            email_recipients=[], send_pagerduty=False, send_email=False,
        )
        assert d.is_routed is True

    def test_is_routed_false_when_rate_limited(self):
        d = RoutingDecision(
            report_id="r1", service="svc", severity="P1",
            channels=[Channel.SLACK], slack_targets=["#incidents"],
            email_recipients=[], send_pagerduty=False, send_email=False,
            rate_limited=True,
        )
        assert d.is_routed is False

    def test_digest_queued_default_false(self):
        d = RoutingDecision(
            report_id="r1", service="svc", severity="P1",
            channels=[], slack_targets=[], email_recipients=[],
            send_pagerduty=False, send_email=False,
        )
        assert d.digest_queued is False

    def test_channel_enum_string_values(self):
        assert Channel.PAGERDUTY == "pagerduty"
        assert Channel.SLACK == "slack"
        assert Channel.EMAIL == "email"


# ═══════════════════════════════════════════════════════════════════════════════
# Routing rules
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingRules:
    def test_p1_rule_has_all_three_channels(self):
        rule = get_rule("P1")
        assert Channel.PAGERDUTY in rule["channels"]
        assert Channel.SLACK in rule["channels"]
        assert Channel.EMAIL in rule["channels"]

    def test_p2_rule_has_slack_and_email_only(self):
        rule = get_rule("P2")
        assert Channel.SLACK in rule["channels"]
        assert Channel.EMAIL in rule["channels"]
        assert Channel.PAGERDUTY not in rule["channels"]

    def test_p3_rule_has_slack_only(self):
        rule = get_rule("P3")
        assert Channel.SLACK in rule["channels"]
        assert Channel.EMAIL not in rule["channels"]
        assert Channel.PAGERDUTY not in rule["channels"]

    def test_p1_slack_channel_is_incidents(self):
        assert "#incidents" in get_rule("P1")["slack_channels"]

    def test_p2_slack_channel_is_alerts(self):
        assert "#alerts" in get_rule("P2")["slack_channels"]

    def test_p3_slack_channel_is_monitoring(self):
        assert "#monitoring" in get_rule("P3")["slack_channels"]

    def test_p1_send_pagerduty_true(self):
        assert get_rule("P1")["send_pagerduty"] is True

    def test_p2_send_pagerduty_false(self):
        assert get_rule("P2")["send_pagerduty"] is False

    def test_p3_send_pagerduty_false(self):
        assert get_rule("P3")["send_pagerduty"] is False

    def test_p1_send_email_true(self):
        assert get_rule("P1")["send_email"] is True

    def test_p2_send_email_true(self):
        assert get_rule("P2")["send_email"] is True

    def test_p3_send_email_false(self):
        assert get_rule("P3")["send_email"] is False

    def test_unknown_severity_falls_back_to_p3(self):
        rule = get_rule("UNKNOWN")
        assert rule == get_rule("P3")

    def test_empty_severity_falls_back_to_p3(self):
        rule = get_rule("")
        assert rule == get_rule("P3")

    def test_severity_rules_covers_all_three_tiers(self):
        assert {"P1", "P2", "P3"}.issubset(SEVERITY_RULES.keys())


# ═══════════════════════════════════════════════════════════════════════════════
# P1 routing
# ═══════════════════════════════════════════════════════════════════════════════


class TestP1Routing:
    def test_returns_routing_decision(self, router):
        result = router.route(_make_report(severity="P1"))
        assert isinstance(result, RoutingDecision)

    def test_p1_includes_pagerduty_channel(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert Channel.PAGERDUTY in decision.channels

    def test_p1_includes_slack_channel(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert Channel.SLACK in decision.channels

    def test_p1_includes_email_channel(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert Channel.EMAIL in decision.channels

    def test_p1_send_pagerduty_true(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert decision.send_pagerduty is True

    def test_p1_send_email_true(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert decision.send_email is True

    def test_p1_slack_targets_include_incidents(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert "#incidents" in decision.slack_targets

    def test_p1_slack_targets_include_oncall_channel(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=_ONCALL_P1))
        assert "#payments-oncall" in decision.slack_targets

    def test_p1_email_recipients_include_oncall(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=_ONCALL_P1))
        assert "payments-oncall@company.com" in decision.email_recipients

    def test_p1_report_id_preserved(self, router):
        report = _make_report(severity="P1")
        decision = router.route(report)
        assert decision.report_id == report.report_id

    def test_p1_service_preserved(self, router):
        decision = router.route(_make_report(severity="P1", service="payment-svc"))
        assert decision.service == "payment-svc"

    def test_p1_severity_preserved(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert decision.severity == "P1"

    def test_p1_not_rate_limited_by_default(self, router):
        decision = router.route(_make_report(severity="P1"))
        assert decision.rate_limited is False
        assert decision.is_routed is True

    def test_p1_three_channels_total(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=None))
        assert len(decision.channels) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# P2 routing
# ═══════════════════════════════════════════════════════════════════════════════


class TestP2Routing:
    def test_p2_does_not_include_pagerduty_channel(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert Channel.PAGERDUTY not in decision.channels

    def test_p2_includes_slack_channel(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert Channel.SLACK in decision.channels

    def test_p2_includes_email_channel(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert Channel.EMAIL in decision.channels

    def test_p2_send_pagerduty_false(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert decision.send_pagerduty is False

    def test_p2_send_email_true(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert decision.send_email is True

    def test_p2_slack_targets_include_alerts(self, router):
        decision = router.route(_make_report(severity="P2"))
        assert "#alerts" in decision.slack_targets

    def test_p2_slack_targets_do_not_include_incidents(self, router):
        decision = router.route(_make_report(severity="P2", oncall_owner=None))
        assert "#incidents" not in decision.slack_targets

    def test_p2_slack_targets_include_oncall_channel(self, router):
        decision = router.route(_make_report(severity="P2", oncall_owner=_ONCALL_P1))
        assert "#payments-oncall" in decision.slack_targets

    def test_p2_email_recipients_include_oncall(self, router):
        decision = router.route(_make_report(severity="P2", oncall_owner=_ONCALL_P1))
        assert "payments-oncall@company.com" in decision.email_recipients

    def test_p2_two_channels(self, router):
        decision = router.route(_make_report(severity="P2", oncall_owner=None))
        assert len(decision.channels) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# P3 routing
# ═══════════════════════════════════════════════════════════════════════════════


class TestP3Routing:
    def test_p3_does_not_include_pagerduty(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert Channel.PAGERDUTY not in decision.channels

    def test_p3_does_not_include_email_channel(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert Channel.EMAIL not in decision.channels

    def test_p3_includes_slack_channel(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert Channel.SLACK in decision.channels

    def test_p3_send_pagerduty_false(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert decision.send_pagerduty is False

    def test_p3_send_email_false(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert decision.send_email is False

    def test_p3_slack_targets_include_monitoring(self, router):
        decision = router.route(_make_report(severity="P3"))
        assert "#monitoring" in decision.slack_targets

    def test_p3_slack_targets_do_not_include_incidents(self, router):
        decision = router.route(_make_report(severity="P3", oncall_owner=None))
        assert "#incidents" not in decision.slack_targets

    def test_p3_slack_targets_do_not_include_alerts(self, router):
        decision = router.route(_make_report(severity="P3", oncall_owner=None))
        assert "#alerts" not in decision.slack_targets

    def test_p3_slack_targets_include_oncall_channel(self, router):
        decision = router.route(_make_report(severity="P3", oncall_owner=_ONCALL_P3))
        assert "#personalization-oncall" in decision.slack_targets

    def test_p3_email_recipients_empty(self, router):
        decision = router.route(_make_report(severity="P3", oncall_owner=_ONCALL_P3))
        assert decision.email_recipients == []

    def test_p3_one_channel(self, router):
        decision = router.route(_make_report(severity="P3", oncall_owner=None))
        assert len(decision.channels) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEdgeCases:
    def test_no_oncall_owner_still_routes(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=None))
        assert isinstance(decision, RoutingDecision)

    def test_no_oncall_owner_email_recipients_empty_for_p1(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=None))
        assert decision.email_recipients == []

    def test_no_oncall_owner_pagerduty_still_flagged_for_p1(self, router):
        decision = router.route(_make_report(severity="P1", oncall_owner=None))
        assert decision.send_pagerduty is True

    def test_oncall_channel_same_as_severity_channel_not_duplicated(self, router):
        """If oncall_owner.slack_channel == '#incidents', it should not appear twice."""
        oncall = {**_ONCALL_P1, "slack_channel": "#incidents"}
        decision = router.route(_make_report(severity="P1", oncall_owner=oncall))
        assert decision.slack_targets.count("#incidents") == 1

    def test_unknown_severity_does_not_raise(self, router):
        decision = router.route(_make_report(severity="P99"))
        assert isinstance(decision, RoutingDecision)

    def test_unknown_severity_treated_as_p3(self, router):
        decision = router.route(_make_report(severity="P99", oncall_owner=None))
        assert decision.send_pagerduty is False
        assert decision.send_email is False
        assert "#monitoring" in decision.slack_targets

    def test_empty_oncall_channel_not_added_to_slack_targets(self, router):
        oncall = {**_ONCALL_P1, "slack_channel": ""}
        decision = router.route(_make_report(severity="P1", oncall_owner=oncall))
        assert "" not in decision.slack_targets

    def test_missing_oncall_email_not_added_to_recipients(self, router):
        oncall = {**_ONCALL_P1, "oncall": ""}
        decision = router.route(_make_report(severity="P1", oncall_owner=oncall))
        assert "" not in decision.email_recipients

    def test_different_services_route_independently(self, router):
        r1 = _make_report(service="payment-svc", severity="P1", oncall_owner=_ONCALL_P1)
        r2 = _make_report(service="recommendation-svc", severity="P3", oncall_owner=_ONCALL_P3)
        d1 = router.route(r1)
        d2 = router.route(r2)
        assert d1.send_pagerduty is True
        assert d2.send_pagerduty is False
        assert "#payments-oncall" in d1.slack_targets
        assert "#personalization-oncall" in d2.slack_targets
        assert "#incidents" not in d2.slack_targets

    def test_channels_list_is_independent_copy(self, router):
        """Mutating the returned channels list must not affect a second call."""
        d1 = router.route(_make_report(severity="P1"))
        d1.channels.clear()
        d2 = router.route(_make_report(severity="P1"))
        assert Channel.PAGERDUTY in d2.channels
