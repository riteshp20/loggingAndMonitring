"""
Unit tests for Phase 4 Part 2 — Slack Block Kit message builders + SlackNotifier.

No real Slack API calls are made — SlackNotifier accepts _client injection.
"""

import uuid
from unittest.mock import MagicMock, call

import pytest

from alert_router.slack.blocks import (
    SlackMessageBuilder,
    _actions_block,
    _context_block,
    _header_block,
    _metrics_block,
    _summary_block,
    _top_errors_block,
    header_text,
)
from alert_router.slack.notifier import SlackNotifier
from report.models import IncidentReport


# ── helpers ───────────────────────────────────────────────────────────────────

_ONCALL = {
    "team": "payments",
    "oncall": "payments-oncall@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD11111",
    "escalation_policy": "payments-escalation",
    "tier": "P1",
}

_LOG_SAMPLES = [
    {"level": "ERROR", "message": "DB timeout"},
    {"level": "ERROR", "message": "DB timeout"},
    {"level": "ERROR", "message": "Connection refused"},
    {"level": "ERROR", "message": "DB timeout"},
]


def _make_report(**overrides) -> IncidentReport:
    defaults = dict(
        report_id=str(uuid.uuid4()),
        event_id=str(uuid.uuid4()),
        service="payment-svc",
        severity="P1",
        anomaly_type="zscore_critical",
        timestamp="2026-04-21T12:00:00+00:00",
        generated_at="2026-04-21T12:01:00+00:00",
        summary="DB pool exhausted. Checkout failing for all users.",
        affected_services=["payment-svc"],
        probable_cause="Pool exhaustion from v2.3.1 deploy.",
        evidence=["error_rate=0.08 vs baseline 0.011", "p99=950ms vs baseline 120ms"],
        suggested_actions=["Roll back to v2.3.0.", "Page DBA."],
        estimated_user_impact="All checkout transactions failing.",
        confidence_score=0.88,
        ai_generated=True,
        oncall_owner=_ONCALL,
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=0,
        top_log_samples=_LOG_SAMPLES,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


def _find_blocks(payload: dict, block_type: str) -> list[dict]:
    return [b for b in payload["blocks"] if b.get("type") == block_type]


def _find_actions_elements(payload: dict) -> list[dict]:
    for block in payload["blocks"]:
        if block.get("type") == "actions":
            return block.get("elements", [])
    return []


@pytest.fixture
def builder() -> SlackMessageBuilder:
    return SlackMessageBuilder(dashboard_base_url="http://opensearch:5601")


@pytest.fixture
def report() -> IncidentReport:
    return _make_report()


# ═══════════════════════════════════════════════════════════════════════════════
# header_text()
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeaderText:
    def test_includes_severity(self, report):
        assert "[P1]" in header_text(report)

    def test_includes_service_name(self, report):
        assert "payment-svc" in header_text(report)

    def test_includes_anomaly_label(self, report):
        # zscore_critical → Spike
        assert "Spike" in header_text(report)

    def test_includes_metric_label(self, report):
        assert "Error Rate" in header_text(report)

    def test_includes_positive_pct_change(self, report):
        # (0.08 - 0.011) / 0.011 ≈ +627%
        text = header_text(report)
        assert "+" in text
        assert "%" in text

    def test_pct_change_value_is_correct(self, report):
        # 627%
        text = header_text(report)
        assert "627" in text

    def test_negative_pct_change_shown_without_plus(self):
        r = _make_report(
            observed_metrics={"error_rate": 0.001},
            baseline_metrics={"error_rate": {"ewma": 0.011}},
        )
        text = header_text(r)
        assert "-" in text
        assert "+" not in text.split("(")[-1]

    def test_system_wide_incident_header(self):
        r = _make_report(anomaly_type="system_wide_incident", severity="P1")
        text = header_text(r)
        assert "System-Wide Incident" in text
        assert "[P1]" in text

    def test_system_wide_incident_no_service_name(self):
        r = _make_report(anomaly_type="system_wide_incident")
        text = header_text(r)
        # The service name should NOT appear for system-wide incidents
        assert "payment-svc" not in text

    def test_unknown_anomaly_type_falls_back_to_anomaly(self):
        r = _make_report(anomaly_type="unknown_type")
        assert "Anomaly" in header_text(r)

    def test_no_baseline_header_still_formed(self):
        r = _make_report(baseline_metrics={}, observed_metrics={"error_rate": 0.08})
        text = header_text(r)
        assert "[P1]" in text
        assert "payment-svc" in text

    def test_p2_severity_in_header(self):
        r = _make_report(severity="P2")
        assert "[P2]" in header_text(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _header_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeaderBlock:
    def test_block_type_is_header(self, report):
        block = _header_block(report)
        assert block["type"] == "header"

    def test_text_type_is_plain_text(self, report):
        block = _header_block(report)
        assert block["text"]["type"] == "plain_text"

    def test_text_matches_header_text(self, report):
        block = _header_block(report)
        assert block["text"]["text"] == header_text(report)

    def test_emoji_enabled(self, report):
        block = _header_block(report)
        assert block["text"]["emoji"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# _metrics_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsBlock:
    def test_block_type_is_section(self, report):
        assert _metrics_block(report)["type"] == "section"

    def test_text_label_is_metrics(self, report):
        assert "Metrics" in _metrics_block(report)["text"]["text"]

    def test_fields_present(self, report):
        assert "fields" in _metrics_block(report)

    def test_first_field_is_metric_header(self, report):
        fields = _metrics_block(report)["fields"]
        assert "Metric" in fields[0]["text"]

    def test_second_field_is_baseline_header(self, report):
        fields = _metrics_block(report)["fields"]
        assert "Baseline" in fields[1]["text"]

    def test_metric_name_appears_in_fields(self, report):
        fields = _metrics_block(report)["fields"]
        texts = [f["text"] for f in fields]
        assert any("Error Rate" in t for t in texts)

    def test_observed_value_appears_in_fields(self, report):
        fields = _metrics_block(report)["fields"]
        texts = [f["text"] for f in fields]
        assert any("0.08" in t for t in texts)

    def test_baseline_value_appears_in_fields(self, report):
        fields = _metrics_block(report)["fields"]
        texts = [f["text"] for f in fields]
        assert any("0.011" in t for t in texts)

    def test_pct_change_appears_in_fields(self, report):
        fields = _metrics_block(report)["fields"]
        texts = [f["text"] for f in fields]
        assert any("627" in t for t in texts)

    def test_fields_all_mrkdwn(self, report):
        fields = _metrics_block(report)["fields"]
        assert all(f["type"] == "mrkdwn" for f in fields)

    def test_empty_metrics_shows_no_data_row(self):
        r = _make_report(observed_metrics={}, baseline_metrics={})
        fields = _metrics_block(r)["fields"]
        texts = [f["text"] for f in fields]
        assert any("No metric data" in t for t in texts)

    def test_fields_count_max_ten(self):
        # 5 metrics → header(2) + 5×2=10 → capped
        metrics = {f"metric_{i}": float(i) / 10 for i in range(1, 6)}
        baselines = {f"metric_{i}": {"ewma": float(i) / 20} for i in range(1, 6)}
        r = _make_report(observed_metrics=metrics, baseline_metrics=baselines)
        assert len(_metrics_block(r)["fields"]) <= 10

    def test_multiple_metrics_all_appear(self):
        r = _make_report(
            observed_metrics={"error_rate": 0.08, "p99_latency_ms": 950.0},
            baseline_metrics={
                "error_rate": {"ewma": 0.011},
                "p99_latency_ms": {"ewma": 120.0},
            },
        )
        texts = [f["text"] for f in _metrics_block(r)["fields"]]
        assert any("Error Rate" in t for t in texts)
        assert any("P99 Latency" in t for t in texts)


# ═══════════════════════════════════════════════════════════════════════════════
# _summary_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestSummaryBlock:
    def test_block_type_is_section(self, report):
        assert _summary_block(report)["type"] == "section"

    def test_contains_summary_text(self, report):
        block = _summary_block(report)
        assert report.summary in block["text"]["text"]

    def test_contains_ai_summary_label(self, report):
        assert "AI Summary" in _summary_block(report)["text"]["text"]

    def test_text_type_is_mrkdwn(self, report):
        assert _summary_block(report)["text"]["type"] == "mrkdwn"


# ═══════════════════════════════════════════════════════════════════════════════
# _top_errors_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestTopErrorsBlock:
    def test_returns_section_block(self, report):
        block = _top_errors_block(report)
        assert block is not None
        assert block["type"] == "section"

    def test_contains_top_errors_label(self, report):
        block = _top_errors_block(report)
        assert "Top Errors" in block["text"]["text"]

    def test_most_frequent_message_appears_first(self, report):
        block = _top_errors_block(report)
        text = block["text"]["text"]
        db_pos = text.find("DB timeout")
        conn_pos = text.find("Connection refused")
        assert db_pos < conn_pos  # DB timeout (×3) before Connection refused (×1)

    def test_count_suffix_shown_for_repeated_messages(self, report):
        block = _top_errors_block(report)
        assert "×3" in block["text"]["text"]

    def test_count_suffix_omitted_for_single_occurrences(self, report):
        block = _top_errors_block(report)
        assert "×1" not in block["text"]["text"]

    def test_max_three_messages_by_default(self):
        samples = [{"level": "ERROR", "message": f"Error {i}"} for i in range(10)]
        r = _make_report(top_log_samples=samples)
        block = _top_errors_block(r)
        assert block is not None
        lines = [l for l in block["text"]["text"].split("\n") if l.startswith(("1.", "2.", "3.", "4."))]
        assert len(lines) <= 3

    def test_returns_none_when_no_log_samples(self):
        r = _make_report(top_log_samples=[])
        assert _top_errors_block(r) is None

    def test_returns_none_when_messages_are_empty_strings(self):
        r = _make_report(top_log_samples=[{"level": "ERROR", "message": ""}])
        assert _top_errors_block(r) is None

    def test_long_messages_are_truncated(self):
        long_msg = "A" * 100
        r = _make_report(top_log_samples=[{"level": "ERROR", "message": long_msg}])
        block = _top_errors_block(r)
        assert block is not None
        assert "…" in block["text"]["text"]

    def test_messages_wrapped_in_code_backticks(self, report):
        block = _top_errors_block(report)
        assert "`DB timeout`" in block["text"]["text"]

    def test_numbered_list_format(self, report):
        text = _top_errors_block(report)["text"]["text"]
        assert "1." in text
        assert "2." in text


# ═══════════════════════════════════════════════════════════════════════════════
# _actions_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestActionsBlock:
    def test_block_type_is_actions(self, report):
        block = _actions_block(report, "http://localhost:5601")
        assert block["type"] == "actions"

    def test_has_three_elements(self, report):
        block = _actions_block(report, "http://localhost:5601")
        assert len(block["elements"]) == 3

    def test_first_button_is_acknowledge(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert "Acknowledge" in elements[0]["text"]["text"]

    def test_second_button_is_view_dashboard(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert "Dashboard" in elements[1]["text"]["text"]

    def test_third_button_is_silence(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert "Silence" in elements[2]["text"]["text"]

    def test_acknowledge_action_id_contains_report_id(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert report.report_id in elements[0]["action_id"]

    def test_silence_button_has_danger_style(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert elements[2]["style"] == "danger"

    def test_dashboard_button_has_url(self, report):
        elements = _actions_block(report, "http://opensearch:5601")["elements"]
        assert "url" in elements[1]

    def test_dashboard_url_contains_base_url(self, report):
        elements = _actions_block(report, "http://opensearch:5601")["elements"]
        assert "opensearch:5601" in elements[1]["url"]

    def test_dashboard_url_contains_service_name(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert "payment-svc" in elements[1]["url"]

    def test_acknowledge_value_prefixed_with_ack(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert elements[0]["value"].startswith("ack_")

    def test_silence_value_prefixed_with_silence(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert elements[2]["value"].startswith("silence_")

    def test_all_buttons_are_type_button(self, report):
        elements = _actions_block(report, "http://localhost:5601")["elements"]
        assert all(e["type"] == "button" for e in elements)


# ═══════════════════════════════════════════════════════════════════════════════
# _context_block()
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBlock:
    def test_block_type_is_context(self, report):
        assert _context_block(report)["type"] == "context"

    def test_contains_confidence_score(self, report):
        text = _context_block(report)["elements"][0]["text"]
        assert "88%" in text

    def test_contains_z_score(self, report):
        text = _context_block(report)["elements"][0]["text"]
        assert "6.2" in text

    def test_contains_pii_scrub_count(self, report):
        r = _make_report(pii_scrub_count=4)
        text = _context_block(r)["elements"][0]["text"]
        assert "4" in text

    def test_ai_generated_shows_checkmark(self, report):
        text = _context_block(report)["elements"][0]["text"]
        assert "✅" in text

    def test_fallback_report_shows_warning(self):
        r = _make_report(ai_generated=False, confidence_score=0.0)
        text = _context_block(r)["elements"][0]["text"]
        assert "fallback" in text

    def test_report_id_prefix_shown(self, report):
        short_id = report.report_id[:8]
        text = _context_block(report)["elements"][0]["text"]
        assert short_id in text


# ═══════════════════════════════════════════════════════════════════════════════
# SlackMessageBuilder.build_alert_message()
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildAlertMessage:
    def test_returns_dict_with_blocks(self, builder, report):
        payload = builder.build_alert_message(report)
        assert "blocks" in payload
        assert isinstance(payload["blocks"], list)

    def test_returns_dict_with_text_fallback(self, builder, report):
        payload = builder.build_alert_message(report)
        assert "text" in payload
        assert "[P1]" in payload["text"]

    def test_has_header_block(self, builder, report):
        payload = builder.build_alert_message(report)
        headers = _find_blocks(payload, "header")
        assert len(headers) == 1

    def test_has_at_least_one_divider(self, builder, report):
        payload = builder.build_alert_message(report)
        assert len(_find_blocks(payload, "divider")) >= 1

    def test_has_actions_block(self, builder, report):
        payload = builder.build_alert_message(report)
        assert len(_find_blocks(payload, "actions")) == 1

    def test_has_context_block(self, builder, report):
        payload = builder.build_alert_message(report)
        assert len(_find_blocks(payload, "context")) == 1

    def test_has_section_blocks(self, builder, report):
        payload = builder.build_alert_message(report)
        assert len(_find_blocks(payload, "section")) >= 2

    def test_header_block_first(self, builder, report):
        payload = builder.build_alert_message(report)
        assert payload["blocks"][0]["type"] == "header"

    def test_three_action_buttons(self, builder, report):
        elements = _find_actions_elements(builder.build_alert_message(report))
        assert len(elements) == 3

    def test_top_errors_block_included_when_samples_present(self, builder, report):
        payload = builder.build_alert_message(report)
        texts = [
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
            if b.get("type") == "section"
        ]
        assert any("Top Errors" in t for t in texts)

    def test_top_errors_block_omitted_when_no_samples(self, builder):
        r = _make_report(top_log_samples=[])
        payload = builder.build_alert_message(r)
        texts = [
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
            if b.get("type") == "section"
        ]
        assert not any("Top Errors" in t for t in texts)

    def test_summary_appears_in_sections(self, builder, report):
        payload = builder.build_alert_message(report)
        sections = _find_blocks(payload, "section")
        texts = [s.get("text", {}).get("text", "") for s in sections]
        assert any(report.summary in t for t in texts)

    def test_dashboard_url_uses_configured_base(self):
        b = SlackMessageBuilder(dashboard_base_url="http://custom-dashboard:3000")
        payload = b.build_alert_message(_make_report())
        elements = _find_actions_elements(payload)
        dashboard_el = next(e for e in elements if "Dashboard" in e["text"]["text"])
        assert "custom-dashboard:3000" in dashboard_el["url"]


# ═══════════════════════════════════════════════════════════════════════════════
# SlackMessageBuilder.build_thread_reply()
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildThreadReply:
    def test_returns_dict_with_blocks(self, builder, report):
        payload = builder.build_thread_reply(report)
        assert "blocks" in payload
        assert isinstance(payload["blocks"], list)

    def test_returns_text_fallback(self, builder, report):
        payload = builder.build_thread_reply(report)
        assert "text" in payload
        assert "payment-svc" in payload["text"]

    def test_title_section_contains_service(self, builder, report):
        payload = builder.build_thread_reply(report)
        first_section = next(b for b in payload["blocks"] if b["type"] == "section")
        assert "payment-svc" in first_section["text"]["text"]

    def test_title_section_contains_severity(self, builder, report):
        payload = builder.build_thread_reply(report)
        first_section = next(b for b in payload["blocks"] if b["type"] == "section")
        assert "P1" in first_section["text"]["text"]

    def test_summary_appears_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        assert report.summary in all_text

    def test_probable_cause_appears_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        assert report.probable_cause in all_text

    def test_evidence_items_appear_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        for item in report.evidence:
            assert item in all_text

    def test_suggested_actions_appear_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        for action in report.suggested_actions:
            assert action in all_text

    def test_user_impact_appears_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        assert report.estimated_user_impact in all_text

    def test_oncall_team_appears_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        assert "payments" in all_text

    def test_pagerduty_id_appears_in_thread(self, builder, report):
        payload = builder.build_thread_reply(report)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
        )
        assert "PD11111" in all_text

    def test_report_id_in_context_block(self, builder, report):
        payload = builder.build_thread_reply(report)
        context_blocks = [b for b in payload["blocks"] if b["type"] == "context"]
        assert len(context_blocks) == 1
        assert report.report_id in context_blocks[0]["elements"][0]["text"]

    def test_confidence_in_context_block(self, builder, report):
        payload = builder.build_thread_reply(report)
        context_blocks = [b for b in payload["blocks"] if b["type"] == "context"]
        assert "88%" in context_blocks[0]["elements"][0]["text"]

    def test_thread_has_no_action_buttons(self, builder, report):
        payload = builder.build_thread_reply(report)
        assert not any(b["type"] == "actions" for b in payload["blocks"])

    def test_thread_for_fallback_report(self, builder):
        r = _make_report(ai_generated=False, confidence_score=0.0)
        payload = builder.build_thread_reply(r)
        context_text = next(
            b["elements"][0]["text"]
            for b in payload["blocks"]
            if b["type"] == "context"
        )
        assert "No (fallback)" in context_text


# ═══════════════════════════════════════════════════════════════════════════════
# SlackNotifier
# ═══════════════════════════════════════════════════════════════════════════════


class TestSlackNotifier:
    def _make_notifier(self, mock_client: MagicMock) -> SlackNotifier:
        mock_client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        return SlackNotifier(
            dashboard_base_url="http://localhost:5601",
            _client=mock_client,
        )

    def test_send_alert_returns_ts(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        ts = notifier.send_alert("#incidents", report)
        assert ts == "1714000000.123456"

    def test_send_alert_calls_postMessage_twice(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        assert client.chat_postMessage.call_count == 2

    def test_first_call_uses_correct_channel(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        first_kwargs = client.chat_postMessage.call_args_list[0][1]
        assert first_kwargs["channel"] == "#incidents"

    def test_second_call_is_thread_reply(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        second_kwargs = client.chat_postMessage.call_args_list[1][1]
        assert second_kwargs["thread_ts"] == "1714000000.123456"

    def test_second_call_same_channel_as_first(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#payments-oncall", report)
        first_kwargs = client.chat_postMessage.call_args_list[0][1]
        second_kwargs = client.chat_postMessage.call_args_list[1][1]
        assert first_kwargs["channel"] == second_kwargs["channel"]

    def test_first_call_has_blocks(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        first_kwargs = client.chat_postMessage.call_args_list[0][1]
        assert "blocks" in first_kwargs

    def test_second_call_has_blocks(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        second_kwargs = client.chat_postMessage.call_args_list[1][1]
        assert "blocks" in second_kwargs

    def test_returns_none_on_exception(self, report):
        client = MagicMock()
        client.chat_postMessage.side_effect = Exception("Slack API down")
        notifier = SlackNotifier(_client=client)
        result = notifier.send_alert("#incidents", report)
        assert result is None

    def test_no_exception_propagated_on_failure(self, report):
        client = MagicMock()
        client.chat_postMessage.side_effect = Exception("network error")
        notifier = SlackNotifier(_client=client)
        notifier.send_alert("#incidents", report)  # must not raise

    def test_returns_none_when_second_call_fails(self, report):
        client = MagicMock()
        client.chat_postMessage.side_effect = [
            {"ts": "1714000000.123456"},
            Exception("thread post failed"),
        ]
        notifier = SlackNotifier(_client=client)
        result = notifier.send_alert("#incidents", report)
        assert result is None

    def test_different_channels_use_independent_calls(self, report):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1714000000.123456"}
        notifier = self._make_notifier(client)
        notifier.send_alert("#incidents", report)
        notifier.send_alert("#alerts", report)
        all_channels = [
            c[1]["channel"] for c in client.chat_postMessage.call_args_list
        ]
        assert "#incidents" in all_channels
        assert "#alerts" in all_channels
