"""
Unit tests for Phase 3 Part 3 — PII scrubbing.

No external services required.  All tests run in-process.
"""

import copy
import uuid
from unittest.mock import MagicMock, patch

import pytest

from models import EnrichedAnomalyEvent, EnrichedContext
from pii.patterns import PATTERNS, Pattern
from pii.scrubber import PiiScrubber, _scrub_value


# ── shared fixtures ───────────────────────────────────────────────────────────

_ONCALL = {
    "team": "payments",
    "oncall": "pay-oncall@company.com",   # should NOT be scrubbed (registry data)
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD123",
}


def _make_event(**overrides) -> EnrichedAnomalyEvent:
    base = dict(
        event_id=str(uuid.uuid4()),
        service="payment-svc",
        anomaly_type="zscore_critical",
        severity="P1",
        z_score=6.2,
        baseline={"error_rate": {"ewma": 0.011, "std_dev": 0.002,
                                  "mean": 0.01, "sample_count": 500,
                                  "last_updated": 1714000000.0}},
        observed={"error_rate": 0.08},
        top_log_samples=[],
        correlated_services=[],
        timestamp="2026-04-21T12:00:00+00:00",
        log_samples=[],
        correlated_anomalies=[],
        recent_deployments=[],
        oncall_owner=_ONCALL,
        pii_scrub_count=0,
    )
    base.update(overrides)
    return EnrichedAnomalyEvent(**base)


@pytest.fixture
def scrubber():
    return PiiScrubber()


# ═══════════════════════════════════════════════════════════════════════════════
# Pattern definitions
# ═══════════════════════════════════════════════════════════════════════════════


class TestPatternDefinitions:
    def test_all_patterns_are_pattern_instances(self):
        for p in PATTERNS:
            assert isinstance(p, Pattern)

    def test_pattern_names_are_unique(self):
        names = [p.name for p in PATTERNS]
        assert len(names) == len(set(names))

    def test_required_pattern_types_present(self):
        names = {p.name for p in PATTERNS}
        assert "email" in names
        assert "credit_card" in names
        assert "ipv4" in names
        assert "ipv6" in names
        assert "phone" in names

    def test_replacements_are_non_empty_strings(self):
        for p in PATTERNS:
            assert isinstance(p.replacement, str)
            assert len(p.replacement) > 0

    def test_replacements_contain_redacted_marker(self):
        for p in PATTERNS:
            assert "REDACTED" in p.replacement.upper()


# ═══════════════════════════════════════════════════════════════════════════════
# scrub_text — individual PII type tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestScrubTextEmails:
    def test_redacts_simple_email(self, scrubber):
        text, n = scrubber.scrub_text("Contact us at user@example.com for help.")
        assert "user@example.com" not in text
        assert "[EMAIL REDACTED]" in text
        assert n == 1

    def test_redacts_email_with_plus(self, scrubber):
        text, n = scrubber.scrub_text("Alert sent to user+alerts@company.co.uk")
        assert "@" not in text or "[EMAIL REDACTED]" in text
        assert n == 1

    def test_redacts_email_with_dots_in_local(self, scrubber):
        text, n = scrubber.scrub_text("first.last@example.org failed login")
        assert "first.last@example.org" not in text
        assert n >= 1

    def test_redacts_multiple_emails(self, scrubber):
        text, n = scrubber.scrub_text(
            "From: alice@corp.com To: bob@corp.com Cc: charlie@corp.com"
        )
        assert n == 3
        assert "alice@corp.com" not in text
        assert "bob@corp.com" not in text
        assert "charlie@corp.com" not in text

    def test_preserves_non_email_at_signs(self, scrubber):
        # A bare "@" with no domain should not be touched
        text, n = scrubber.scrub_text("Twitter handle @johndoe mentioned something")
        assert "@johndoe" in text
        assert n == 0


class TestScrubTextCreditCards:
    def test_redacts_visa_16_digit(self, scrubber):
        text, n = scrubber.scrub_text("Card: 4532015112830366 charged")
        assert "4532015112830366" not in text
        assert "[CC REDACTED]" in text
        assert n == 1

    def test_redacts_visa_with_dashes(self, scrubber):
        text, n = scrubber.scrub_text("Visa: 4111-1111-1111-1111")
        assert "4111-1111-1111-1111" not in text
        assert n == 1

    def test_redacts_visa_with_spaces(self, scrubber):
        text, n = scrubber.scrub_text("Card 4111 1111 1111 1111 processed")
        assert "4111 1111 1111 1111" not in text
        assert n == 1

    def test_redacts_mastercard(self, scrubber):
        text, n = scrubber.scrub_text("MC: 5500005555555559 declined")
        assert "5500005555555559" not in text
        assert n == 1

    def test_redacts_amex(self, scrubber):
        text, n = scrubber.scrub_text("Amex 378282246310005 expired")
        assert "378282246310005" not in text
        assert n == 1

    def test_redacts_amex_grouped(self, scrubber):
        text, n = scrubber.scrub_text("Amex 3782-822463-10005 auth failed")
        assert "3782-822463-10005" not in text
        assert n == 1

    def test_redacts_discover(self, scrubber):
        text, n = scrubber.scrub_text("Discover: 6011111111111117 charged")
        assert "6011111111111117" not in text
        assert n == 1


class TestScrubTextPhones:
    def test_redacts_us_phone_dashes(self, scrubber):
        text, n = scrubber.scrub_text("Call 555-867-5309 for support")
        assert "555-867-5309" not in text
        assert "[PHONE REDACTED]" in text
        assert n == 1

    def test_redacts_phone_with_parentheses(self, scrubber):
        text, n = scrubber.scrub_text("Reached (800) 123-4567 ext 10")
        assert "(800) 123-4567" not in text
        assert n == 1

    def test_redacts_phone_with_dots(self, scrubber):
        text, n = scrubber.scrub_text("Callback: 415.555.0100")
        assert "415.555.0100" not in text
        assert n == 1

    def test_redacts_phone_with_country_code(self, scrubber):
        text, n = scrubber.scrub_text("+1 800 555-1234 called")
        assert "+1 800 555-1234" not in text
        assert n >= 1

    def test_does_not_redact_short_digit_sequences(self, scrubber):
        # Port numbers, HTTP status codes, etc.
        text, n = scrubber.scrub_text("Port 8080 returned 404 after 200ms")
        assert n == 0

    def test_does_not_redact_version_numbers(self, scrubber):
        text, n = scrubber.scrub_text("Version 2.3.1 deployed successfully")
        assert n == 0


class TestScrubTextIPv4:
    def test_redacts_standard_ipv4(self, scrubber):
        text, n = scrubber.scrub_text("Request from 192.168.1.100 failed")
        assert "192.168.1.100" not in text
        assert "[IPv4 REDACTED]" in text
        assert n == 1

    def test_redacts_public_ipv4(self, scrubber):
        text, n = scrubber.scrub_text("Origin IP: 203.0.113.42")
        assert "203.0.113.42" not in text
        assert n == 1

    def test_redacts_localhost(self, scrubber):
        text, n = scrubber.scrub_text("Connected to 127.0.0.1:5432")
        assert "127.0.0.1" not in text
        assert n == 1

    def test_redacts_multiple_ipv4(self, scrubber):
        text, n = scrubber.scrub_text(
            "Traffic from 10.0.0.1 routed via 172.16.0.254 to 192.168.100.5"
        )
        assert n == 3

    def test_does_not_redact_invalid_octet(self, scrubber):
        # 999 is not a valid octet
        text, n = scrubber.scrub_text("Code 999.999.999.999 invalid")
        assert n == 0


class TestScrubTextIPv6:
    def test_redacts_full_ipv6(self, scrubber):
        addr = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
        text, n = scrubber.scrub_text(f"Source: {addr}")
        assert addr not in text
        assert "[IPv6 REDACTED]" in text
        assert n == 1

    def test_redacts_compressed_ipv6(self, scrubber):
        text, n = scrubber.scrub_text("Addr: 2001:db8::1")
        assert "2001:db8::1" not in text
        assert n >= 1

    def test_redacts_loopback_ipv6(self, scrubber):
        text, n = scrubber.scrub_text("Loopback ::1 connected")
        assert "::1" not in text
        assert n >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# scrub_text — general behaviour
# ═══════════════════════════════════════════════════════════════════════════════


class TestScrubTextGeneral:
    def test_preserves_non_pii_text(self, scrubber):
        clean = "DB connection pool exhausted after 30000ms on payment-svc"
        text, n = scrubber.scrub_text(clean)
        assert text == clean
        assert n == 0

    def test_returns_zero_count_for_clean_text(self, scrubber):
        _, n = scrubber.scrub_text("The service returned HTTP 500.")
        assert n == 0

    def test_counts_multiple_pii_types(self, scrubber):
        mixed = (
            "User alice@corp.com from 10.0.0.5 "
            "used card 4532015112830366 and called 555-867-5309"
        )
        _, n = scrubber.scrub_text(mixed)
        assert n == 4  # email + ipv4 + cc + phone

    def test_empty_string(self, scrubber):
        text, n = scrubber.scrub_text("")
        assert text == ""
        assert n == 0

    def test_replacement_tokens_not_re_scrubbed(self, scrubber):
        # Applying scrubber twice must not produce nested REDACTED tokens
        text1, _ = scrubber.scrub_text("user@example.com")
        text2, n2 = scrubber.scrub_text(text1)
        assert n2 == 0
        assert "[EMAIL REDACTED]" in text2


# ═══════════════════════════════════════════════════════════════════════════════
# _scrub_value helper — recursive structure traversal
# ═══════════════════════════════════════════════════════════════════════════════


class TestScrubValueHelper:
    def test_scrubs_plain_string(self):
        result, n = _scrub_value("Contact user@example.com for info")
        assert "user@example.com" not in result
        assert n == 1

    def test_scrubs_string_values_in_dict(self):
        record = {"message": "Error for user@corp.com", "level": "ERROR"}
        result, n = _scrub_value(record)
        assert "user@corp.com" not in result["message"]
        assert result["level"] == "ERROR"
        assert n == 1

    def test_scrubs_nested_dict(self):
        record = {
            "outer": {
                "inner": "Request from 192.168.1.1 failed"
            }
        }
        result, n = _scrub_value(record)
        assert "192.168.1.1" not in result["outer"]["inner"]
        assert n == 1

    def test_scrubs_list_of_strings(self):
        records = ["user1@test.com said hello", "normal text", "user2@test.com logged in"]
        result, n = _scrub_value(records)
        assert n == 2
        assert "user1@test.com" not in result[0]
        assert result[1] == "normal text"

    def test_preserves_numeric_values(self):
        record = {"error_rate": 0.08, "count": 500}
        result, n = _scrub_value(record)
        assert result == record
        assert n == 0

    def test_preserves_none_values(self):
        result, n = _scrub_value(None)
        assert result is None
        assert n == 0

    def test_preserves_bool_values(self):
        result, n = _scrub_value(True)
        assert result is True
        assert n == 0

    def test_dict_keys_not_scrubbed(self):
        # Keys shouldn't be modified even if they look like PII
        record = {"user@test.com": "some value"}
        result, n = _scrub_value(record)
        assert "user@test.com" in result  # key preserved
        assert n == 0  # value "some value" has no PII


# ═══════════════════════════════════════════════════════════════════════════════
# PiiScrubber.scrub_event
# ═══════════════════════════════════════════════════════════════════════════════


class TestScrubEvent:
    def test_scrubs_log_samples(self, scrubber):
        event = _make_event(log_samples=[
            {"message": "Auth failed for user@corp.com", "log_level": "ERROR",
             "@timestamp": "2026-04-21T12:00:00Z", "service_name": "payment-svc"}
        ])
        result = scrubber.scrub_event(event)
        assert "user@corp.com" not in result.log_samples[0]["message"]
        assert "[EMAIL REDACTED]" in result.log_samples[0]["message"]

    def test_scrubs_top_log_samples(self, scrubber):
        event = _make_event(top_log_samples=[
            {"message": "Request from 10.0.0.5 failed", "level": "ERROR"}
        ])
        result = scrubber.scrub_event(event)
        assert "10.0.0.5" not in result.top_log_samples[0]["message"]

    def test_scrubs_recent_deployments(self, scrubber):
        event = _make_event(recent_deployments=[
            {"service": "payment-svc", "version": "v2.3.1",
             "deployed_by": "deployer@company.com",
             "timestamp": "2026-04-21T10:00:00Z"}
        ])
        result = scrubber.scrub_event(event)
        assert "deployer@company.com" not in result.recent_deployments[0]["deployed_by"]

    def test_scrubs_correlated_anomalies_text(self, scrubber):
        event = _make_event(correlated_anomalies=[
            {"service": "auth-svc", "severity": "P2",
             "message": "Failed login from 203.0.113.50"}
        ])
        result = scrubber.scrub_event(event)
        assert "203.0.113.50" not in str(result.correlated_anomalies)

    def test_updates_pii_scrub_count(self, scrubber):
        event = _make_event(log_samples=[
            {"message": "user@test.com from 192.168.0.1 card 4532015112830366"}
        ])
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 3  # email + ipv4 + cc

    def test_accumulates_pii_scrub_count_on_existing(self, scrubber):
        event = _make_event(
            pii_scrub_count=5,  # already had some scrubs
            log_samples=[{"message": "user@test.com"}]
        )
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 6  # 5 + 1 new

    def test_does_not_mutate_original_event(self, scrubber):
        original_msg = "Login for user@corp.com from 10.0.0.1"
        event = _make_event(log_samples=[
            {"message": original_msg, "level": "ERROR"}
        ])
        scrubber.scrub_event(event)
        # Original must be untouched
        assert event.log_samples[0]["message"] == original_msg
        assert event.pii_scrub_count == 0

    def test_does_not_scrub_oncall_owner(self, scrubber):
        event = _make_event(oncall_owner={
            "team": "payments",
            "oncall": "pay-oncall@company.com",
            "slack_channel": "#payments-oncall",
        })
        result = scrubber.scrub_event(event)
        # On-call contact preserved — needed to page the right team
        assert result.oncall_owner["oncall"] == "pay-oncall@company.com"

    def test_does_not_scrub_baseline_metrics(self, scrubber):
        event = _make_event()
        original_baseline = copy.deepcopy(event.baseline)
        result = scrubber.scrub_event(event)
        assert result.baseline == original_baseline

    def test_does_not_scrub_observed_metrics(self, scrubber):
        event = _make_event()
        original_observed = copy.deepcopy(event.observed)
        result = scrubber.scrub_event(event)
        assert result.observed == original_observed

    def test_returns_enriched_anomaly_event_type(self, scrubber):
        event = _make_event()
        result = scrubber.scrub_event(event)
        assert isinstance(result, EnrichedAnomalyEvent)

    def test_pii_scrub_count_zero_when_nothing_scrubbed(self, scrubber):
        event = _make_event(log_samples=[
            {"message": "Service started successfully", "level": "INFO"}
        ])
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 0

    def test_scrubs_multiple_records(self, scrubber):
        event = _make_event(log_samples=[
            {"message": "user1@test.com failed"},
            {"message": "user2@test.com failed"},
            {"message": "no pii here"},
        ])
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 2
        assert "[EMAIL REDACTED]" in result.log_samples[0]["message"]
        assert "[EMAIL REDACTED]" in result.log_samples[1]["message"]
        assert "no pii here" == result.log_samples[2]["message"]

    def test_scrubs_pii_across_multiple_fields(self, scrubber):
        event = _make_event(
            log_samples=[{"message": "user@test.com error"}],
            top_log_samples=[{"message": "addr 10.0.0.1 blocked"}],
            recent_deployments=[{"deployed_by": "dev@corp.com"}],
        )
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 3  # email + ipv4 + email

    def test_empty_fields_produce_zero_count(self, scrubber):
        event = _make_event(
            log_samples=[],
            top_log_samples=[],
            correlated_anomalies=[],
            recent_deployments=[],
        )
        result = scrubber.scrub_event(event)
        assert result.pii_scrub_count == 0

    def test_non_string_values_in_records_preserved(self, scrubber):
        event = _make_event(log_samples=[
            {"message": "ok", "count": 42, "flag": True, "ratio": 0.05, "extra": None}
        ])
        result = scrubber.scrub_event(event)
        rec = result.log_samples[0]
        assert rec["count"] == 42
        assert rec["flag"] is True
        assert rec["ratio"] == pytest.approx(0.05)
        assert rec["extra"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: PiiScrubber wired into ClaudeReportGenerator
# ═══════════════════════════════════════════════════════════════════════════════


class TestClaudeGeneratorWithScrubber:
    """Verify the scrubber runs before prompt-building inside ClaudeReportGenerator."""

    def _valid_response(self) -> MagicMock:
        import json as _json
        body = _json.dumps({
            "summary": "Payment service anomaly. DB timeouts causing failures.",
            "severity": "P1",
            "affected_services": ["payment-svc"],
            "probable_cause": "DB pool exhaustion.",
            "evidence": ["error_rate=0.08"],
            "suggested_actions": ["Roll back deployment."],
            "estimated_user_impact": "Checkout failures.",
            "confidence_score": 0.85,
        })
        content = MagicMock()
        content.text = body
        response = MagicMock()
        response.content = [content]
        return response

    def test_pii_scrub_count_in_report_reflects_scrubbing(self):
        from report.claude_client import ClaudeReportGenerator

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._valid_response()

        scrubber = PiiScrubber()
        gen = ClaudeReportGenerator(_client=mock_client, pii_scrubber=scrubber)

        event = _make_event(log_samples=[
            {"message": "Auth failed for user@corp.com from 10.0.0.1",
             "log_level": "ERROR", "@timestamp": "2026-04-21T12:00:00Z",
             "service_name": "payment-svc"}
        ])
        report = gen.generate(event)
        assert report.pii_scrub_count == 2  # email + ipv4

    def test_scrubber_called_before_prompt_is_built(self):
        from report.claude_client import ClaudeReportGenerator

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._valid_response()

        scrubber = PiiScrubber()
        gen = ClaudeReportGenerator(_client=mock_client, pii_scrubber=scrubber)

        event = _make_event(log_samples=[
            {"message": "user secret@pii.com logged in", "log_level": "INFO",
             "@timestamp": "t", "service_name": "svc"}
        ])
        gen.generate(event)

        # The prompt sent to Claude must not contain the raw email
        _, kwargs = mock_client.messages.create.call_args
        user_prompt = kwargs["messages"][0]["content"]
        assert "secret@pii.com" not in user_prompt

    def test_generate_without_scrubber_does_not_raise(self):
        from report.claude_client import ClaudeReportGenerator

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._valid_response()

        gen = ClaudeReportGenerator(_client=mock_client)  # no scrubber
        event = _make_event()
        report = gen.generate(event)
        assert report is not None
