"""
Unit tests for Phase 4 Part 3 — PagerDuty integration.

No real HTTP calls are made — PagerDutyClient accepts _post_fn injection.
"""

import urllib.error
import uuid
from unittest.mock import MagicMock, call

import pytest

from alert_router.pagerduty.client import (
    RESOLVE_Z_SCORE_THRESHOLD,
    PagerDutyClient,
    _EVENTS_URL,
    dedup_key,
)
from alert_router.pagerduty.models import (
    PagerDutyAction,
    PagerDutySeverity,
    pd_severity,
)
from report.models import IncidentReport


# ── helpers ───────────────────────────────────────────────────────────────────

_ONCALL_WITH_RUNBOOK = {
    "team": "payments",
    "oncall": "payments-oncall@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD11111",
    "escalation_policy": "payments-escalation",
    "tier": "P1",
    "runbook_url": "https://wiki.company.com/runbooks/payment-svc",
}

_ONCALL_NO_RUNBOOK = {
    "team": "platform",
    "oncall": "oncall@company.com",
    "slack_channel": "#platform-oncall",
    "pagerduty_service_id": None,
    "escalation_policy": "default-escalation",
    "tier": "P3",
    "runbook_url": "",
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
        probable_cause="Pool exhaustion from v2.3.1.",
        evidence=["error_rate=0.08"],
        suggested_actions=["Roll back."],
        estimated_user_impact="All checkouts failing.",
        confidence_score=0.88,
        ai_generated=True,
        oncall_owner=_ONCALL_WITH_RUNBOOK,
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=0,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


def _make_client(post_fn=None) -> PagerDutyClient:
    if post_fn is None:
        post_fn = MagicMock(return_value={"status": "success", "dedup_key": "k"})
    return PagerDutyClient(routing_key="test-routing-key", _post_fn=post_fn)


# ═══════════════════════════════════════════════════════════════════════════════
# dedup_key()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDedupKey:
    def test_format_is_service_underscore_anomaly_type(self):
        assert dedup_key("payment-svc", "zscore_critical") == "payment-svc_zscore_critical"

    def test_includes_service(self):
        assert "payment-svc" in dedup_key("payment-svc", "both_tiers")

    def test_includes_anomaly_type(self):
        assert "both_tiers" in dedup_key("payment-svc", "both_tiers")

    def test_different_services_produce_different_keys(self):
        assert dedup_key("auth-svc", "zscore_critical") != dedup_key("payment-svc", "zscore_critical")

    def test_different_anomaly_types_produce_different_keys(self):
        assert dedup_key("payment-svc", "zscore_critical") != dedup_key("payment-svc", "both_tiers")

    def test_same_inputs_produce_same_key(self):
        assert dedup_key("payment-svc", "zscore_critical") == dedup_key("payment-svc", "zscore_critical")

    def test_system_wide_incident_key(self):
        key = dedup_key("system", "system_wide_incident")
        assert key == "system_system_wide_incident"


# ═══════════════════════════════════════════════════════════════════════════════
# PagerDutyAction / PagerDutySeverity enums
# ═══════════════════════════════════════════════════════════════════════════════


class TestPagerDutyEnums:
    def test_trigger_action_value(self):
        assert PagerDutyAction.TRIGGER == "trigger"

    def test_resolve_action_value(self):
        assert PagerDutyAction.RESOLVE == "resolve"

    def test_critical_severity_value(self):
        assert PagerDutySeverity.CRITICAL == "critical"

    def test_error_severity_value(self):
        assert PagerDutySeverity.ERROR == "error"

    def test_warning_severity_value(self):
        assert PagerDutySeverity.WARNING == "warning"

    def test_info_severity_value(self):
        assert PagerDutySeverity.INFO == "info"


# ═══════════════════════════════════════════════════════════════════════════════
# pd_severity()
# ═══════════════════════════════════════════════════════════════════════════════


class TestPdSeverity:
    def test_p1_maps_to_critical(self):
        assert pd_severity("P1") == PagerDutySeverity.CRITICAL

    def test_p2_maps_to_error(self):
        assert pd_severity("P2") == PagerDutySeverity.ERROR

    def test_p3_maps_to_warning(self):
        assert pd_severity("P3") == PagerDutySeverity.WARNING

    def test_unknown_severity_maps_to_warning(self):
        assert pd_severity("P99") == PagerDutySeverity.WARNING

    def test_empty_severity_maps_to_warning(self):
        assert pd_severity("") == PagerDutySeverity.WARNING

    def test_returns_pagerduty_severity_instance(self):
        assert isinstance(pd_severity("P1"), PagerDutySeverity)


# ═══════════════════════════════════════════════════════════════════════════════
# Trigger payload structure
# ═══════════════════════════════════════════════════════════════════════════════


class TestTriggerPayload:
    def _payload(self, **overrides) -> dict:
        client = _make_client()
        return client._build_trigger_payload(_make_report(**overrides))

    def test_event_action_is_trigger(self):
        assert self._payload()["event_action"] == "trigger"

    def test_routing_key_present(self):
        assert self._payload()["routing_key"] == "test-routing-key"

    def test_dedup_key_format(self):
        p = self._payload()
        assert p["dedup_key"] == "payment-svc_zscore_critical"

    def test_dedup_key_uses_report_service(self):
        p = self._payload(service="auth-svc")
        assert p["dedup_key"].startswith("auth-svc")

    def test_dedup_key_uses_report_anomaly_type(self):
        p = self._payload(anomaly_type="both_tiers")
        assert p["dedup_key"].endswith("both_tiers")

    def test_payload_summary_matches_report(self):
        report = _make_report()
        p = _make_client()._build_trigger_payload(report)
        assert p["payload"]["summary"] == report.summary

    def test_payload_severity_is_critical_for_p1(self):
        assert self._payload(severity="P1")["payload"]["severity"] == "critical"

    def test_payload_severity_is_error_for_p2(self):
        assert self._payload(severity="P2")["payload"]["severity"] == "error"

    def test_payload_severity_is_warning_for_p3(self):
        assert self._payload(severity="P3")["payload"]["severity"] == "warning"

    def test_payload_source_is_service(self):
        assert self._payload()["payload"]["source"] == "payment-svc"

    def test_payload_timestamp_matches_report(self):
        report = _make_report()
        p = _make_client()._build_trigger_payload(report)
        assert p["payload"]["timestamp"] == report.timestamp

    def test_custom_details_has_report_id(self):
        report = _make_report()
        details = _make_client()._build_trigger_payload(report)["payload"]["custom_details"]
        assert details["report_id"] == report.report_id

    def test_custom_details_has_z_score(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["z_score"] == pytest.approx(6.2)

    def test_custom_details_has_anomaly_type(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["anomaly_type"] == "zscore_critical"

    def test_custom_details_has_observed_metrics(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["observed_metrics"] == {"error_rate": 0.08}

    def test_custom_details_has_probable_cause(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["probable_cause"] == "Pool exhaustion from v2.3.1."

    def test_custom_details_has_affected_services(self):
        details = self._payload()["payload"]["custom_details"]
        assert "payment-svc" in details["affected_services"]

    def test_custom_details_has_confidence_score(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["confidence_score"] == pytest.approx(0.88)

    def test_custom_details_has_ai_generated_flag(self):
        details = self._payload()["payload"]["custom_details"]
        assert details["ai_generated"] is True

    def test_client_field_present(self):
        assert self._payload()["client"] == "incident-reporter"

    def test_links_contains_runbook_url(self):
        links = self._payload()["links"]
        assert len(links) == 1
        assert links[0]["href"] == "https://wiki.company.com/runbooks/payment-svc"

    def test_links_runbook_text_label(self):
        links = self._payload()["links"]
        assert links[0]["text"] == "Runbook"

    def test_links_empty_when_no_runbook_url(self):
        p = self._payload(oncall_owner=_ONCALL_NO_RUNBOOK)
        assert p["links"] == []

    def test_links_empty_when_oncall_owner_none(self):
        p = self._payload(oncall_owner=None)
        assert p["links"] == []

    def test_payload_is_json_serialisable(self):
        import json
        payload = self._payload()
        json.dumps(payload)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Resolve payload structure
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolvePayload:
    def _payload(self, service="payment-svc", anomaly_type="zscore_critical") -> dict:
        return _make_client()._build_resolve_payload(service, anomaly_type)

    def test_event_action_is_resolve(self):
        assert self._payload()["event_action"] == "resolve"

    def test_routing_key_present(self):
        assert self._payload()["routing_key"] == "test-routing-key"

    def test_dedup_key_matches_trigger_key(self):
        resolve_key = self._payload()["dedup_key"]
        trigger_key = dedup_key("payment-svc", "zscore_critical")
        assert resolve_key == trigger_key

    def test_resolve_payload_has_no_payload_field(self):
        assert "payload" not in self._payload()

    def test_resolve_payload_has_no_links_field(self):
        assert "links" not in self._payload()

    def test_different_service_produces_different_dedup_key(self):
        k1 = self._payload("payment-svc")["dedup_key"]
        k2 = self._payload("auth-svc")["dedup_key"]
        assert k1 != k2


# ═══════════════════════════════════════════════════════════════════════════════
# PagerDutyClient.trigger()
# ═══════════════════════════════════════════════════════════════════════════════


class TestClientTrigger:
    def test_trigger_calls_post_fn_once(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).trigger(_make_report())
        post.assert_called_once()

    def test_trigger_posts_to_events_url(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).trigger(_make_report())
        url = post.call_args[0][0]
        assert url == _EVENTS_URL

    def test_trigger_payload_has_trigger_action(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).trigger(_make_report())
        payload = post.call_args[0][1]
        assert payload["event_action"] == "trigger"

    def test_trigger_returns_true_on_success(self):
        post = MagicMock(return_value={"status": "success"})
        assert _make_client(post).trigger(_make_report()) is True

    def test_trigger_returns_false_on_http_error(self):
        post = MagicMock(side_effect=urllib.error.HTTPError(
            url=_EVENTS_URL, code=400, msg="Bad Request", hdrs=None, fp=None
        ))
        assert _make_client(post).trigger(_make_report()) is False

    def test_trigger_returns_false_on_network_error(self):
        post = MagicMock(side_effect=OSError("connection refused"))
        assert _make_client(post).trigger(_make_report()) is False

    def test_trigger_does_not_raise_on_error(self):
        post = MagicMock(side_effect=Exception("timeout"))
        _make_client(post).trigger(_make_report())  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# PagerDutyClient.resolve()
# ═══════════════════════════════════════════════════════════════════════════════


class TestClientResolve:
    def test_resolve_calls_post_fn_once(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).resolve("payment-svc", "zscore_critical")
        post.assert_called_once()

    def test_resolve_posts_to_events_url(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).resolve("payment-svc", "zscore_critical")
        url = post.call_args[0][0]
        assert url == _EVENTS_URL

    def test_resolve_payload_has_resolve_action(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).resolve("payment-svc", "zscore_critical")
        payload = post.call_args[0][1]
        assert payload["event_action"] == "resolve"

    def test_resolve_dedup_key_matches_trigger_dedup_key(self):
        post = MagicMock(return_value={"status": "success"})
        _make_client(post).resolve("payment-svc", "zscore_critical")
        payload = post.call_args[0][1]
        assert payload["dedup_key"] == dedup_key("payment-svc", "zscore_critical")

    def test_resolve_returns_true_on_success(self):
        post = MagicMock(return_value={"status": "success"})
        assert _make_client(post).resolve("payment-svc", "zscore_critical") is True

    def test_resolve_returns_false_on_http_error(self):
        post = MagicMock(side_effect=urllib.error.HTTPError(
            url=_EVENTS_URL, code=500, msg="Server Error", hdrs=None, fp=None
        ))
        assert _make_client(post).resolve("payment-svc", "zscore_critical") is False

    def test_resolve_does_not_raise_on_error(self):
        post = MagicMock(side_effect=Exception("network down"))
        _make_client(post).resolve("payment-svc", "zscore_critical")  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# PagerDutyClient.notify() — trigger vs resolve routing
# ═══════════════════════════════════════════════════════════════════════════════


class TestNotifyRouting:
    def test_notify_triggers_when_z_score_above_threshold(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.notify(_make_report(z_score=RESOLVE_Z_SCORE_THRESHOLD + 0.1))
        payload = post.call_args[0][1]
        assert payload["event_action"] == "trigger"

    def test_notify_resolves_when_z_score_below_threshold(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.notify(_make_report(z_score=RESOLVE_Z_SCORE_THRESHOLD - 0.1))
        payload = post.call_args[0][1]
        assert payload["event_action"] == "resolve"

    def test_notify_triggers_when_z_score_exactly_at_threshold(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.notify(_make_report(z_score=RESOLVE_Z_SCORE_THRESHOLD))
        payload = post.call_args[0][1]
        # z == threshold is NOT below threshold → trigger
        assert payload["event_action"] == "trigger"

    def test_notify_resolve_uses_correct_dedup_key(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        report = _make_report(z_score=0.5, service="auth-svc", anomaly_type="both_tiers")
        client.notify(report)
        payload = post.call_args[0][1]
        assert payload["dedup_key"] == dedup_key("auth-svc", "both_tiers")

    def test_notify_trigger_uses_correct_dedup_key(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        report = _make_report(z_score=5.0, service="auth-svc", anomaly_type="both_tiers")
        client.notify(report)
        payload = post.call_args[0][1]
        assert payload["dedup_key"] == dedup_key("auth-svc", "both_tiers")

    def test_notify_returns_true_on_success(self):
        post = MagicMock(return_value={"status": "success"})
        assert _make_client(post).notify(_make_report(z_score=6.0)) is True

    def test_notify_returns_false_on_failure(self):
        post = MagicMock(side_effect=Exception("API down"))
        assert _make_client(post).notify(_make_report(z_score=6.0)) is False

    def test_threshold_constant_is_1_5(self):
        assert RESOLVE_Z_SCORE_THRESHOLD == 1.5

    def test_zero_z_score_resolves(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.notify(_make_report(z_score=0.0))
        payload = post.call_args[0][1]
        assert payload["event_action"] == "resolve"

    def test_high_z_score_triggers(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.notify(_make_report(z_score=10.0))
        payload = post.call_args[0][1]
        assert payload["event_action"] == "trigger"


# ═══════════════════════════════════════════════════════════════════════════════
# Runbook URL from service registry
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunbookUrl:
    def test_runbook_url_included_in_links_when_present(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.trigger(_make_report(oncall_owner=_ONCALL_WITH_RUNBOOK))
        payload = post.call_args[0][1]
        assert len(payload["links"]) == 1
        assert payload["links"][0]["href"] == "https://wiki.company.com/runbooks/payment-svc"

    def test_runbook_url_excluded_when_empty_string(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.trigger(_make_report(oncall_owner=_ONCALL_NO_RUNBOOK))
        payload = post.call_args[0][1]
        assert payload["links"] == []

    def test_runbook_url_excluded_when_key_missing(self):
        oncall_no_key = {k: v for k, v in _ONCALL_WITH_RUNBOOK.items() if k != "runbook_url"}
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.trigger(_make_report(oncall_owner=oncall_no_key))
        payload = post.call_args[0][1]
        assert payload["links"] == []

    def test_runbook_url_excluded_when_oncall_owner_none(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.trigger(_make_report(oncall_owner=None))
        payload = post.call_args[0][1]
        assert payload["links"] == []

    def test_runbook_link_text_is_runbook(self):
        post = MagicMock(return_value={"status": "success"})
        client = _make_client(post)
        client.trigger(_make_report(oncall_owner=_ONCALL_WITH_RUNBOOK))
        payload = post.call_args[0][1]
        assert payload["links"][0]["text"] == "Runbook"

    def test_different_services_have_different_runbook_urls(self):
        auth_oncall = {**_ONCALL_WITH_RUNBOOK, "runbook_url": "https://wiki.company.com/runbooks/auth-svc"}
        post1 = MagicMock(return_value={"status": "success"})
        post2 = MagicMock(return_value={"status": "success"})
        _make_client(post1).trigger(_make_report(oncall_owner=_ONCALL_WITH_RUNBOOK))
        _make_client(post2).trigger(_make_report(oncall_owner=auth_oncall))
        url1 = post1.call_args[0][1]["links"][0]["href"]
        url2 = post2.call_args[0][1]["links"][0]["href"]
        assert url1 != url2


# ═══════════════════════════════════════════════════════════════════════════════
# Service registry integration (reads real service_registry.json)
# ═══════════════════════════════════════════════════════════════════════════════


class TestServiceRegistryRunbooks:
    def test_registry_has_runbook_url_for_payment_svc(self):
        from enrichment.registry import ServiceRegistry
        reg = ServiceRegistry("service_registry.json")
        owner = reg.get_oncall_owner("payment-svc")
        assert owner is not None
        assert "runbook_url" in owner
        assert owner["runbook_url"] != ""

    def test_registry_has_runbook_url_for_all_services(self):
        from enrichment.registry import ServiceRegistry
        reg = ServiceRegistry("service_registry.json")
        for svc in reg.all_services():
            owner = reg.get_oncall_owner(svc)
            assert "runbook_url" in owner, f"{svc} missing runbook_url"

    def test_registry_default_has_runbook_url(self):
        from enrichment.registry import ServiceRegistry
        reg = ServiceRegistry("service_registry.json")
        owner = reg.get_oncall_owner("unknown-service-xyz")
        assert owner is not None
        assert "runbook_url" in owner

    def test_runbook_urls_contain_service_name(self):
        from enrichment.registry import ServiceRegistry
        reg = ServiceRegistry("service_registry.json")
        for svc in reg.all_services():
            owner = reg.get_oncall_owner(svc)
            assert svc in owner["runbook_url"], (
                f"Expected {svc} in runbook URL but got: {owner['runbook_url']}"
            )
