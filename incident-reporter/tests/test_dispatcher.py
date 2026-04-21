"""
Unit tests for Phase 4 Part 4 — AlertDispatcher (full wiring).

All notifiers are mocked; rate limiter and digest buffer are real instances
with a fake clock so tests are deterministic.
"""

import uuid
from unittest.mock import MagicMock, call

import pytest

from alert_router.digest import DigestBuffer
from alert_router.dispatcher import AlertDispatcher, DispatchResult, _DIGEST_CHANNEL
from alert_router.models import Channel
from alert_router.pagerduty.client import PagerDutyClient
from alert_router.rate_limiter import MAX_ALERTS_PER_WINDOW, RateLimiter
from alert_router.router import AlertRouter
from alert_router.slack.notifier import SlackNotifier
from report.models import IncidentReport


# ── helpers ───────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self._t = t

    def __call__(self) -> float:
        return self._t

    def advance(self, s: float) -> None:
        self._t += s


_ONCALL_P1 = {
    "team": "payments",
    "oncall": "pay@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD11111",
    "escalation_policy": "pay-esc",
    "tier": "P1",
    "runbook_url": "https://wiki.company.com/runbooks/payment-svc",
}


def _make_report(severity: str = "P1", service: str = "payment-svc", **overrides) -> IncidentReport:
    defaults = dict(
        report_id=str(uuid.uuid4()),
        event_id=str(uuid.uuid4()),
        service=service,
        severity=severity,
        anomaly_type="zscore_critical",
        timestamp="2026-04-21T12:00:00+00:00",
        generated_at="2026-04-21T12:01:00+00:00",
        summary="DB pool exhausted. Checkout failing.",
        affected_services=[service],
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


def _mock_slack() -> MagicMock:
    """Returns a MagicMock that behaves like SlackNotifier."""
    m = MagicMock(spec=SlackNotifier)
    m.send_alert.return_value = "1714000000.123"
    m.send_message.return_value = "1714000001.000"
    # Provide a builder with a real build_digest_message
    from alert_router.slack.blocks import SlackMessageBuilder
    m._builder = SlackMessageBuilder()
    return m


def _mock_pd() -> MagicMock:
    m = MagicMock(spec=PagerDutyClient)
    m.notify.return_value = True
    return m


def _make_dispatcher(
    clock: FakeClock | None = None,
    slack: MagicMock | None = None,
    pd: MagicMock | None = None,
    max_alerts: int = MAX_ALERTS_PER_WINDOW,
) -> tuple[AlertDispatcher, DigestBuffer]:
    if clock is None:
        clock = FakeClock()
    digest = DigestBuffer()
    dispatcher = AlertDispatcher(
        router=AlertRouter(),
        rate_limiter=RateLimiter(max_alerts=max_alerts, _now_fn=clock),
        digest=digest,
        slack_notifier=slack if slack is not None else _mock_slack(),
        pd_client=pd if pd is not None else _mock_pd(),
    )
    return dispatcher, digest


# ═══════════════════════════════════════════════════════════════════════════════
# DispatchResult model
# ═══════════════════════════════════════════════════════════════════════════════


class TestDispatchResult:
    def test_default_digest_pending_count_is_zero(self):
        r = DispatchResult(
            report_id="r", service="s", severity="P1",
            channels_dispatched=[], slack_targets_notified=[],
            pagerduty_triggered=False, rate_limited=False, digest_queued=False,
        )
        assert r.digest_pending_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — happy path
# ═══════════════════════════════════════════════════════════════════════════════


class TestDispatchP1HappyPath:
    def test_returns_dispatch_result(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1"))
        assert isinstance(result, DispatchResult)

    def test_not_rate_limited(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1"))
        assert result.rate_limited is False

    def test_not_digest_queued(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1"))
        assert result.digest_queued is False

    def test_pagerduty_triggered(self):
        pd = _mock_pd()
        d, _ = _make_dispatcher(pd=pd)
        result = d.dispatch(_make_report(severity="P1"))
        assert result.pagerduty_triggered is True
        pd.notify.assert_called_once()

    def test_slack_channels_notified(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        result = d.dispatch(_make_report(severity="P1"))
        assert len(result.slack_targets_notified) > 0
        assert slack.send_alert.called

    def test_incidents_channel_in_slack_targets(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        d.dispatch(_make_report(severity="P1"))
        channels_called = [c[0][0] for c in slack.send_alert.call_args_list]
        assert "#incidents" in channels_called

    def test_oncall_channel_in_slack_targets(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        d.dispatch(_make_report(severity="P1"))
        channels_called = [c[0][0] for c in slack.send_alert.call_args_list]
        assert "#payments-oncall" in channels_called

    def test_pagerduty_channel_in_channels_dispatched(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1"))
        assert Channel.PAGERDUTY in result.channels_dispatched

    def test_report_id_preserved(self):
        report = _make_report(severity="P1")
        d, _ = _make_dispatcher()
        result = d.dispatch(report)
        assert result.report_id == report.report_id

    def test_service_preserved(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1", service="auth-svc"))
        assert result.service == "auth-svc"

    def test_severity_preserved(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P1"))
        assert result.severity == "P1"


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — no PagerDuty
# ═══════════════════════════════════════════════════════════════════════════════


class TestDispatchP2:
    def test_pagerduty_not_triggered_for_p2(self):
        pd = _mock_pd()
        d, _ = _make_dispatcher(pd=pd)
        result = d.dispatch(_make_report(severity="P2"))
        assert result.pagerduty_triggered is False
        pd.notify.assert_not_called()

    def test_slack_called_for_p2(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        d.dispatch(_make_report(severity="P2"))
        assert slack.send_alert.called

    def test_alerts_channel_notified_for_p2(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        d.dispatch(_make_report(severity="P2"))
        channels_called = [c[0][0] for c in slack.send_alert.call_args_list]
        assert "#alerts" in channels_called

    def test_no_pagerduty_channel_in_dispatched_for_p2(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P2"))
        assert Channel.PAGERDUTY not in result.channels_dispatched


# ═══════════════════════════════════════════════════════════════════════════════
# P3 — Slack only
# ═══════════════════════════════════════════════════════════════════════════════


class TestDispatchP3:
    def test_pagerduty_not_triggered_for_p3(self):
        pd = _mock_pd()
        d, _ = _make_dispatcher(pd=pd)
        result = d.dispatch(_make_report(severity="P3"))
        assert result.pagerduty_triggered is False

    def test_monitoring_channel_notified_for_p3(self):
        slack = _mock_slack()
        d, _ = _make_dispatcher(slack=slack)
        d.dispatch(_make_report(severity="P3"))
        channels_called = [c[0][0] for c in slack.send_alert.call_args_list]
        assert "#monitoring" in channels_called

    def test_no_email_channel_in_dispatched_for_p3(self):
        d, _ = _make_dispatcher()
        result = d.dispatch(_make_report(severity="P3"))
        assert Channel.EMAIL not in result.channels_dispatched


# ═══════════════════════════════════════════════════════════════════════════════
# Rate limiting
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimiting:
    def test_tenth_alert_is_dispatched(self):
        clock = FakeClock()
        slack = _mock_slack()
        d, _ = _make_dispatcher(clock=clock, slack=slack, max_alerts=10)
        for _ in range(10):
            result = d.dispatch(_make_report())
        assert result.rate_limited is False

    def test_eleventh_alert_is_rate_limited(self):
        clock = FakeClock()
        d, _ = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        result = d.dispatch(_make_report())
        assert result.rate_limited is True

    def test_rate_limited_result_has_digest_queued_true(self):
        clock = FakeClock()
        d, _ = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        result = d.dispatch(_make_report())
        assert result.digest_queued is True

    def test_rate_limited_result_has_empty_channels(self):
        clock = FakeClock()
        d, _ = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        result = d.dispatch(_make_report())
        assert result.channels_dispatched == []

    def test_pagerduty_not_called_when_rate_limited(self):
        clock = FakeClock()
        pd = _mock_pd()
        d, _ = _make_dispatcher(clock=clock, pd=pd, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        pd.reset_mock()
        d.dispatch(_make_report())
        pd.notify.assert_not_called()

    def test_slack_not_called_when_rate_limited(self):
        clock = FakeClock()
        slack = _mock_slack()
        d, _ = _make_dispatcher(clock=clock, slack=slack, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        slack.reset_mock()
        d.dispatch(_make_report())
        slack.send_alert.assert_not_called()

    def test_rate_limited_report_added_to_digest(self):
        clock = FakeClock()
        d, digest = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        report = _make_report()
        d.dispatch(report)
        assert digest.pending_count("payment-svc") == 1

    def test_digest_pending_count_in_result(self):
        clock = FakeClock()
        d, digest = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        result = d.dispatch(_make_report())
        assert result.digest_pending_count == 1

    def test_multiple_excess_alerts_accumulate_in_digest(self):
        clock = FakeClock()
        d, digest = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        for _ in range(5):
            d.dispatch(_make_report())
        assert digest.pending_count("payment-svc") == 5

    def test_different_services_rate_limited_independently(self):
        clock = FakeClock()
        d, digest = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report(service="payment-svc"))
        result_other = d.dispatch(_make_report(service="auth-svc"))
        assert result_other.rate_limited is False

    def test_rate_limit_resets_after_window(self):
        from alert_router.rate_limiter import WINDOW_SECONDS
        clock = FakeClock()
        d, _ = _make_dispatcher(clock=clock, max_alerts=10)
        for _ in range(10):
            d.dispatch(_make_report())
        clock.advance(WINDOW_SECONDS + 1)
        result = d.dispatch(_make_report())
        assert result.rate_limited is False


# ═══════════════════════════════════════════════════════════════════════════════
# send_digest()
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendDigest:
    def _fill_digest(self, n: int = 3) -> tuple[AlertDispatcher, DigestBuffer, MagicMock]:
        clock = FakeClock()
        slack = _mock_slack()
        d, digest = _make_dispatcher(clock=clock, slack=slack, max_alerts=1)
        d.dispatch(_make_report())  # allowed
        for _ in range(n):
            d.dispatch(_make_report())  # rate-limited → digest
        slack.reset_mock()
        return d, digest, slack

    def test_send_digest_returns_true_when_reports_flushed(self):
        d, _, _ = self._fill_digest()
        assert d.send_digest("payment-svc") is True

    def test_send_digest_posts_to_slack(self):
        d, _, slack = self._fill_digest()
        d.send_digest("payment-svc")
        slack.send_message.assert_called_once()

    def test_send_digest_posts_to_monitoring_by_default(self):
        d, _, slack = self._fill_digest()
        d.send_digest("payment-svc")
        channel_arg = slack.send_message.call_args[0][0]
        assert channel_arg == "#monitoring"

    def test_send_digest_posts_to_custom_channel(self):
        d, _, slack = self._fill_digest()
        d.send_digest("payment-svc", channel="#custom-alerts")
        channel_arg = slack.send_message.call_args[0][0]
        assert channel_arg == "#custom-alerts"

    def test_send_digest_clears_buffer(self):
        d, digest, _ = self._fill_digest(3)
        d.send_digest("payment-svc")
        assert digest.pending_count("payment-svc") == 0

    def test_send_digest_returns_false_when_buffer_empty(self):
        d, _ = _make_dispatcher()
        assert d.send_digest("payment-svc") is False

    def test_send_digest_returns_false_when_no_slack(self):
        clock = FakeClock()
        digest = DigestBuffer()
        d = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(max_alerts=1, _now_fn=clock),
            digest=digest,
            slack_notifier=None,
            pd_client=None,
        )
        d.dispatch(_make_report())
        d.dispatch(_make_report())  # rate-limited
        assert d.send_digest("payment-svc") is False

    def test_send_all_digests_sends_for_each_service(self):
        clock = FakeClock()
        slack = _mock_slack()
        d, _ = _make_dispatcher(clock=clock, slack=slack, max_alerts=1)
        d.dispatch(_make_report(service="svc-a"))
        d.dispatch(_make_report(service="svc-a"))  # rate-limited
        d.dispatch(_make_report(service="svc-b"))
        d.dispatch(_make_report(service="svc-b"))  # rate-limited
        slack.reset_mock()
        results = d.send_all_digests()
        assert "svc-a" in results
        assert "svc-b" in results
        assert slack.send_message.call_count == 2

    def test_send_all_digests_empty_when_no_pending(self):
        d, _ = _make_dispatcher()
        results = d.send_all_digests()
        assert results == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Optional notifiers (None)
# ═══════════════════════════════════════════════════════════════════════════════


class TestOptionalNotifiers:
    def test_dispatch_without_slack_notifier_does_not_raise(self):
        clock = FakeClock()
        d, _ = _make_dispatcher(clock=clock, slack=None, pd=_mock_pd())
        d.slack_notifier = None  # force None
        # rebuild without slack
        d2 = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(_now_fn=clock),
            digest=DigestBuffer(),
            slack_notifier=None,
            pd_client=_mock_pd(),
        )
        result = d2.dispatch(_make_report())
        assert result.slack_targets_notified == []

    def test_dispatch_without_pd_client_does_not_raise(self):
        d2 = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(),
            digest=DigestBuffer(),
            slack_notifier=_mock_slack(),
            pd_client=None,
        )
        result = d2.dispatch(_make_report(severity="P1"))
        assert result.pagerduty_triggered is False

    def test_dispatch_without_any_notifiers_does_not_raise(self):
        d2 = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(),
            digest=DigestBuffer(),
            slack_notifier=None,
            pd_client=None,
        )
        result = d2.dispatch(_make_report())
        assert isinstance(result, DispatchResult)

    def test_slack_failure_does_not_stop_dispatch(self):
        slack = _mock_slack()
        slack.send_alert.return_value = None  # simulate failure
        d2 = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(),
            digest=DigestBuffer(),
            slack_notifier=slack,
            pd_client=_mock_pd(),
        )
        result = d2.dispatch(_make_report(severity="P1"))
        # PD still triggered even if Slack failed
        assert result.pagerduty_triggered is True
        assert result.slack_targets_notified == []

    def test_pd_failure_does_not_stop_slack(self):
        pd = _mock_pd()
        pd.notify.return_value = False
        slack = _mock_slack()
        d2 = AlertDispatcher(
            router=AlertRouter(),
            rate_limiter=RateLimiter(),
            digest=DigestBuffer(),
            slack_notifier=slack,
            pd_client=pd,
        )
        result = d2.dispatch(_make_report(severity="P1"))
        assert result.pagerduty_triggered is False
        assert slack.send_alert.called
