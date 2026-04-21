"""
Unit tests for Phase 3 Part 2 — Claude API report generation.

No real Anthropic API required:
  - ClaudeReportGenerator → injected MagicMock via _client=
  - time.sleep → patched to avoid retry delays
"""

import json
import uuid
from unittest.mock import MagicMock, call, patch

import httpx
import pytest
import anthropic

from models import EnrichedAnomalyEvent, EnrichedContext
from report.models import IncidentReport
from report.prompt_builder import build_user_message, get_system_prompt
from report.claude_client import ClaudeReportGenerator, _extract_json, _clamp


# ── shared fixtures ───────────────────────────────────────────────────────────

_DUMMY_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
_RESP_429 = httpx.Response(429, request=_DUMMY_REQ)
_RESP_500 = httpx.Response(500, request=_DUMMY_REQ)
_RESP_401 = httpx.Response(401, request=_DUMMY_REQ)
_RESP_400 = httpx.Response(400, request=_DUMMY_REQ)


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("Too many requests", response=_RESP_429, body=None)


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_DUMMY_REQ)


def _timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(_DUMMY_REQ)


def _internal_server_error() -> anthropic.InternalServerError:
    return anthropic.InternalServerError("Server error", response=_RESP_500, body=None)


def _auth_error() -> anthropic.AuthenticationError:
    return anthropic.AuthenticationError("Invalid key", response=_RESP_401, body=None)


def _bad_request_error() -> anthropic.BadRequestError:
    return anthropic.BadRequestError("Bad input", response=_RESP_400, body=None)


_VALID_AI_RESPONSE = json.dumps({
    "summary": "Payment service error rate spiked to 8%, 6σ above baseline. DB connection pool exhausted after deploy v2.3.1.",
    "severity": "P1",
    "affected_services": ["payment-svc", "order-svc"],
    "probable_cause": "DB connection pool exhaustion triggered by misconfigured pool size in v2.3.1 deployment.",
    "evidence": [
        "error_rate observed=0.0800 vs baseline_ewma=0.0110 (z=6.20)",
        "p99_latency_ms observed=950.0 vs baseline 250.0",
        "Deployment v2.3.1 released 2 hours before anomaly",
        "Log samples show repeated DB connection timeout errors",
    ],
    "suggested_actions": [
        "Roll back payment-svc to v2.3.0 immediately.",
        "Increase DB connection pool size as emergency mitigation.",
        "Check DB server CPU and connection count.",
        "Page on-call DBA to investigate DB health.",
    ],
    "estimated_user_impact": "Checkout failures for all users attempting payment. High severity — P1.",
    "confidence_score": 0.88,
})

_ONCALL = {
    "team": "payments",
    "oncall": "pay-oncall@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD123",
    "escalation_policy": "pay-escalation",
    "tier": "P1",
}


def _make_enriched_event(**overrides) -> EnrichedAnomalyEvent:
    base = dict(
        event_id=str(uuid.uuid4()),
        service="payment-svc",
        anomaly_type="zscore_critical",
        severity="P1",
        z_score=6.2,
        baseline={"error_rate": {"mean": 0.01, "std_dev": 0.002, "ewma": 0.011,
                                  "sample_count": 500, "last_updated": 1714000000.0}},
        observed={"error_rate": 0.08, "p99_latency_ms": 950.0},
        top_log_samples=[{"level": "ERROR", "message": "DB timeout", "@timestamp": "2026-04-21T12:00:00Z"}],
        correlated_services=[],
        timestamp="2026-04-21T12:00:00+00:00",
        log_samples=[{"@timestamp": "2026-04-21T12:00:00Z", "log_level": "ERROR",
                      "message": "DB connection timeout after 30000ms", "service_name": "payment-svc"}],
        correlated_anomalies=[],
        recent_deployments=[{"service": "payment-svc", "version": "v2.3.1",
                              "timestamp": "2026-04-21T10:00:00Z", "deployed_by": "ci-pipeline"}],
        oncall_owner=_ONCALL,
        pii_scrub_count=0,
    )
    base.update(overrides)
    return EnrichedAnomalyEvent(**base)


SAMPLE_EVENT = _make_enriched_event()


def _mock_claude_response(text: str) -> MagicMock:
    """Build a mock that looks like anthropic.Message."""
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.messages.create.return_value = _mock_claude_response(_VALID_AI_RESPONSE)
    return client


@pytest.fixture
def generator(mock_client):
    return ClaudeReportGenerator(_client=mock_client)


# ═══════════════════════════════════════════════════════════════════════════════
# IncidentReport model
# ═══════════════════════════════════════════════════════════════════════════════


class TestIncidentReportModel:
    def _make_report(self, **kw) -> IncidentReport:
        defaults = dict(
            report_id=str(uuid.uuid4()), event_id="evt-1", service="svc",
            severity="P1", anomaly_type="zscore_critical",
            timestamp="2026-04-21T12:00:00+00:00", generated_at="2026-04-21T12:01:00+00:00",
            summary="Something broke. Users impacted.", affected_services=["svc"],
            probable_cause="High z-score", evidence=["e1"], suggested_actions=["act1"],
            estimated_user_impact="checkout failures", confidence_score=0.9,
            ai_generated=True, oncall_owner=_ONCALL, z_score=6.2,
            observed_metrics={"error_rate": 0.08}, baseline_metrics={},
        )
        defaults.update(kw)
        return IncidentReport(**defaults)

    def test_report_id_is_uuid(self):
        r = self._make_report()
        uuid.UUID(r.report_id)  # raises if invalid

    def test_to_dict_contains_all_required_fields(self):
        r = self._make_report()
        d = r.to_dict()
        required = {
            "report_id", "event_id", "service", "severity", "anomaly_type",
            "timestamp", "generated_at", "summary", "affected_services",
            "probable_cause", "evidence", "suggested_actions",
            "estimated_user_impact", "confidence_score", "ai_generated",
            "oncall_owner", "z_score", "observed_metrics", "baseline_metrics",
            "pii_scrub_count", "top_log_samples",
        }
        assert required == set(d.keys())

    def test_to_json_round_trips(self):
        r = self._make_report()
        parsed = json.loads(r.to_json())
        assert parsed["service"] == "svc"
        assert parsed["confidence_score"] == pytest.approx(0.9)
        assert parsed["ai_generated"] is True

    def test_pii_scrub_count_defaults_to_zero(self):
        r = self._make_report()
        assert r.pii_scrub_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt builder
# ═══════════════════════════════════════════════════════════════════════════════


class TestSystemPrompt:
    def test_contains_sre_persona(self):
        p = get_system_prompt()
        assert "Site Reliability Engineer" in p or "SRE" in p

    def test_specifies_json_only_output(self):
        p = get_system_prompt()
        assert "JSON" in p
        assert "markdown" in p.lower() or "fences" in p.lower() or "preamble" in p.lower()

    def test_lists_all_required_fields(self):
        p = get_system_prompt()
        for field in [
            "summary", "severity", "affected_services", "probable_cause",
            "evidence", "suggested_actions", "estimated_user_impact", "confidence_score",
        ]:
            assert field in p, f"System prompt missing field: {field}"

    def test_specifies_confidence_score_range(self):
        p = get_system_prompt()
        assert "0.0" in p or "0–1" in p or "0.0–1.0" in p

    def test_instructs_two_sentence_summary(self):
        p = get_system_prompt()
        assert "2 sentences" in p or "two sentences" in p.lower()


class TestUserMessageBuilder:
    def test_contains_service_name(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "payment-svc" in msg

    def test_contains_severity(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "P1" in msg

    def test_contains_anomaly_type(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "zscore_critical" in msg

    def test_contains_z_score(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "6.20" in msg or "6.2" in msg

    def test_contains_observed_metrics(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "error_rate" in msg
        assert "0.0800" in msg or "0.08" in msg

    def test_contains_baseline_ewma(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "0.0110" in msg or "0.011" in msg

    def test_contains_log_samples(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "DB connection timeout" in msg

    def test_log_samples_section_header_present(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "LOG SAMPLES" in msg

    def test_contains_deployment_info(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "v2.3.1" in msg

    def test_contains_oncall_info(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "#payments-oncall" in msg or "payments" in msg

    def test_section_headers_present(self):
        msg = build_user_message(SAMPLE_EVENT)
        assert "ANOMALY ALERT" in msg
        assert "METRICS" in msg

    def test_optional_sections_absent_when_no_data(self):
        event = _make_enriched_event(
            log_samples=[],
            top_log_samples=[],
            correlated_anomalies=[],
            correlated_services=[],
            recent_deployments=[],
            oncall_owner=None,
        )
        msg = build_user_message(event)
        assert "LOG SAMPLES" not in msg
        assert "CORRELATED ANOMALIES" not in msg
        assert "RECENT DEPLOYMENTS" not in msg
        assert "ON-CALL" not in msg

    def test_falls_back_to_top_log_samples_when_os_samples_empty(self):
        event = _make_enriched_event(
            log_samples=[],
            top_log_samples=[{"level": "ERROR", "message": "fallback log", "@timestamp": "t"}],
        )
        msg = build_user_message(event)
        assert "fallback log" in msg

    def test_prefers_opensearch_samples_over_top_log_samples(self):
        event = _make_enriched_event(
            log_samples=[{"log_level": "ERROR", "message": "opensearch log", "@timestamp": "t",
                          "service_name": "svc"}],
            top_log_samples=[{"level": "ERROR", "message": "detector log", "@timestamp": "t"}],
        )
        msg = build_user_message(event)
        assert "opensearch log" in msg
        # When OS samples exist the source label says OpenSearch
        assert "OpenSearch" in msg

    def test_correlated_anomaly_section_shows_service_and_severity(self):
        event = _make_enriched_event(
            correlated_anomalies=[{
                "service": "auth-svc", "severity": "P2",
                "anomaly_type": "both_tiers", "timestamp": "2026-04-21T11:59:00+00:00",
                "z_score": 3.8,
            }]
        )
        msg = build_user_message(event)
        assert "auth-svc" in msg
        assert "P2" in msg
        assert "CORRELATED ANOMALIES" in msg

    def test_log_samples_capped_at_20(self):
        logs = [{"@timestamp": f"t{i}", "log_level": "ERROR",
                 "message": f"msg{i}", "service_name": "svc"} for i in range(30)]
        event = _make_enriched_event(log_samples=logs)
        msg = build_user_message(event)
        # The 21st log should not appear
        assert "[21]" not in msg
        assert "[20]" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# JSON extraction helper
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractJson:
    def test_parses_pure_json(self):
        raw = '{"summary": "ok", "confidence_score": 0.9}'
        assert _extract_json(raw) == {"summary": "ok", "confidence_score": 0.9}

    def test_strips_markdown_json_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        assert _extract_json(raw) == {"key": "value"}

    def test_strips_markdown_fence_without_language(self):
        raw = '```\n{"key": "value"}\n```'
        assert _extract_json(raw) == {"key": "value"}

    def test_extracts_json_from_prose(self):
        raw = 'Here is my analysis: {"summary": "boom"} — that is all.'
        assert _extract_json(raw) == {"summary": "boom"}

    def test_handles_whitespace_padding(self):
        raw = '   \n  {"k": 1}  \n  '
        assert _extract_json(raw) == {"k": 1}

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("This is just prose with no JSON at all.")

    def test_raises_on_broken_json(self):
        with pytest.raises(ValueError):
            _extract_json("{broken: json content}")


class TestClampHelper:
    def test_clamps_above_one(self):
        assert _clamp(1.5) == 1.0

    def test_clamps_below_zero(self):
        assert _clamp(-0.3) == 0.0

    def test_preserves_valid_values(self):
        assert _clamp(0.75) == pytest.approx(0.75)

    def test_zero_and_one_edges(self):
        assert _clamp(0.0) == 0.0
        assert _clamp(1.0) == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# ClaudeReportGenerator — happy path
# ═══════════════════════════════════════════════════════════════════════════════


class TestClaudeReportGeneratorSuccess:
    def test_returns_incident_report(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert isinstance(result, IncidentReport)

    def test_ai_generated_true_on_success(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.ai_generated is True

    def test_report_id_is_uuid(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        uuid.UUID(result.report_id)  # raises ValueError if not valid

    def test_event_id_linked_to_anomaly_event(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.event_id == SAMPLE_EVENT.event_id

    def test_service_matches_event(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.service == "payment-svc"

    def test_summary_populated_from_claude(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert len(result.summary) > 0
        assert "payment" in result.summary.lower() or "error" in result.summary.lower()

    def test_severity_from_claude_response(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.severity in ("P1", "P2", "P3")

    def test_affected_services_is_list(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert isinstance(result.affected_services, list)

    def test_evidence_is_list_of_strings(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert isinstance(result.evidence, list)
        assert all(isinstance(e, str) for e in result.evidence)

    def test_suggested_actions_is_list(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert isinstance(result.suggested_actions, list)
        assert len(result.suggested_actions) > 0

    def test_confidence_score_in_range(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert 0.0 <= result.confidence_score <= 1.0

    def test_confidence_score_from_claude(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.confidence_score == pytest.approx(0.88)

    def test_oncall_owner_preserved(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.oncall_owner == _ONCALL

    def test_observed_metrics_preserved(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.observed_metrics == SAMPLE_EVENT.observed

    def test_baseline_metrics_preserved(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.baseline_metrics == SAMPLE_EVENT.baseline

    def test_z_score_preserved(self, generator):
        result = generator.generate(SAMPLE_EVENT)
        assert result.z_score == pytest.approx(6.2)

    def test_claude_called_with_correct_model(self, generator, mock_client):
        generator.generate(SAMPLE_EVENT)
        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["model"] == "claude-sonnet-4-20250514"

    def test_claude_called_with_max_tokens_800(self, generator, mock_client):
        generator.generate(SAMPLE_EVENT)
        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["max_tokens"] == 800

    def test_claude_called_with_temperature_0_2(self, generator, mock_client):
        generator.generate(SAMPLE_EVENT)
        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["temperature"] == pytest.approx(0.2)

    def test_claude_called_with_system_prompt(self, generator, mock_client):
        generator.generate(SAMPLE_EVENT)
        _, kwargs = mock_client.messages.create.call_args
        assert "SRE" in kwargs["system"] or "Site Reliability" in kwargs["system"]

    def test_claude_called_with_user_message_containing_service(self, generator, mock_client):
        generator.generate(SAMPLE_EVENT)
        _, kwargs = mock_client.messages.create.call_args
        user_content = kwargs["messages"][0]["content"]
        assert "payment-svc" in user_content

    def test_claude_response_parses_json_from_markdown_fence(self, mock_client):
        fenced = f"```json\n{_VALID_AI_RESPONSE}\n```"
        mock_client.messages.create.return_value = _mock_claude_response(fenced)
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is True
        assert result.confidence_score == pytest.approx(0.88)

    def test_confidence_score_clamped_above_1(self, mock_client):
        data = json.loads(_VALID_AI_RESPONSE)
        data["confidence_score"] = 1.5  # out of range
        mock_client.messages.create.return_value = _mock_claude_response(json.dumps(data))
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.confidence_score == 1.0

    def test_confidence_score_clamped_below_0(self, mock_client):
        data = json.loads(_VALID_AI_RESPONSE)
        data["confidence_score"] = -0.2
        mock_client.messages.create.return_value = _mock_claude_response(json.dumps(data))
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.confidence_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Retry logic
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryLogic:
    @patch("report.claude_client.time.sleep")
    def test_retries_on_rate_limit(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = [
            _rate_limit_error(),
            _mock_claude_response(_VALID_AI_RESPONSE),
        ]
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is True
        assert mock_client.messages.create.call_count == 2

    @patch("report.claude_client.time.sleep")
    def test_retries_on_connection_error(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = [
            _connection_error(),
            _mock_claude_response(_VALID_AI_RESPONSE),
        ]
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is True
        assert mock_client.messages.create.call_count == 2

    @patch("report.claude_client.time.sleep")
    def test_retries_on_timeout(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = [
            _timeout_error(),
            _mock_claude_response(_VALID_AI_RESPONSE),
        ]
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is True
        assert mock_client.messages.create.call_count == 2

    @patch("report.claude_client.time.sleep")
    def test_retries_on_internal_server_error(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = [
            _internal_server_error(),
            _mock_claude_response(_VALID_AI_RESPONSE),
        ]
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is True

    @patch("report.claude_client.time.sleep")
    def test_retry_sleep_is_exponential(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = [
            _rate_limit_error(),  # attempt 0 → sleep 1s
            _rate_limit_error(),  # attempt 1 → sleep 2s
            _mock_claude_response(_VALID_AI_RESPONSE),
        ]
        gen = ClaudeReportGenerator(_client=mock_client)
        gen.generate(SAMPLE_EVENT)
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [1, 2]  # 2^0, 2^1

    @patch("report.claude_client.time.sleep")
    def test_does_not_retry_on_auth_error(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = _auth_error()
        gen = ClaudeReportGenerator(_client=mock_client)
        # Auth error is non-retryable; generate() catches it and falls back
        result = gen.generate(SAMPLE_EVENT)
        assert mock_client.messages.create.call_count == 1
        assert result.ai_generated is False  # fell back

    @patch("report.claude_client.time.sleep")
    def test_does_not_retry_on_bad_request(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = _bad_request_error()
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert mock_client.messages.create.call_count == 1
        assert result.ai_generated is False

    @patch("report.claude_client.time.sleep")
    def test_exhausts_all_retries_then_falls_back(self, mock_sleep, mock_client):
        mock_client.messages.create.side_effect = _rate_limit_error()
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert mock_client.messages.create.call_count == 3  # _MAX_RETRIES
        assert result.ai_generated is False


# ═══════════════════════════════════════════════════════════════════════════════
# Fallback report
# ═══════════════════════════════════════════════════════════════════════════════


class TestFallbackReport:
    def _get_fallback(self) -> IncidentReport:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API down")
        gen = ClaudeReportGenerator(_client=mock_client)
        return gen.generate(SAMPLE_EVENT)

    def test_ai_generated_is_false(self):
        assert self._get_fallback().ai_generated is False

    def test_confidence_score_is_zero(self):
        assert self._get_fallback().confidence_score == 0.0

    def test_summary_mentions_service_and_anomaly_type(self):
        r = self._get_fallback()
        assert "payment-svc" in r.summary
        assert "zscore_critical" in r.summary

    def test_affected_services_includes_trigger_service(self):
        r = self._get_fallback()
        assert "payment-svc" in r.affected_services

    def test_evidence_derived_from_observed_metrics(self):
        r = self._get_fallback()
        assert len(r.evidence) > 0
        assert any("error_rate" in e for e in r.evidence)
        assert any("p99_latency_ms" in e for e in r.evidence)

    def test_suggested_actions_not_empty(self):
        r = self._get_fallback()
        assert len(r.suggested_actions) >= 4

    def test_all_required_fields_present(self):
        r = self._get_fallback()
        d = r.to_dict()
        for field in ["summary", "severity", "affected_services", "probable_cause",
                      "evidence", "suggested_actions", "estimated_user_impact",
                      "confidence_score"]:
            assert field in d

    def test_fallback_has_valid_report_id(self):
        r = self._get_fallback()
        uuid.UUID(r.report_id)

    def test_fallback_includes_correlated_services(self):
        event = _make_enriched_event(correlated_services=["auth-svc", "order-svc"])
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("down")
        gen = ClaudeReportGenerator(_client=mock_client)
        r = gen.generate(event)
        assert "auth-svc" in r.affected_services
        assert "order-svc" in r.affected_services

    def test_fallback_on_invalid_json_from_claude(self, mock_client):
        mock_client.messages.create.return_value = _mock_claude_response(
            "I cannot analyze this alert at this time."
        )
        gen = ClaudeReportGenerator(_client=mock_client)
        result = gen.generate(SAMPLE_EVENT)
        assert result.ai_generated is False
        assert result.confidence_score == 0.0
