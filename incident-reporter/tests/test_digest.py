"""
Unit tests for Phase 4 Part 4 — DigestBuffer and digest Slack message builder.
"""

import threading
import uuid

import pytest

from alert_router.digest import DigestBuffer
from alert_router.slack.blocks import SlackMessageBuilder
from report.models import IncidentReport


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_report(service: str = "payment-svc", **overrides) -> IncidentReport:
    defaults = dict(
        report_id=str(uuid.uuid4()),
        event_id=str(uuid.uuid4()),
        service=service,
        severity="P1",
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
        oncall_owner=None,
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=0,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


@pytest.fixture
def buf() -> DigestBuffer:
    return DigestBuffer()


@pytest.fixture
def builder() -> SlackMessageBuilder:
    return SlackMessageBuilder()


# ═══════════════════════════════════════════════════════════════════════════════
# DigestBuffer.add()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestBufferAdd:
    def test_add_returns_one_for_first_report(self, buf):
        r = _make_report()
        assert buf.add(r) == 1

    def test_add_returns_incremented_count(self, buf):
        r1, r2, r3 = _make_report(), _make_report(), _make_report()
        buf.add(r1)
        buf.add(r2)
        assert buf.add(r3) == 3

    def test_different_services_counted_independently(self, buf):
        ra = _make_report(service="svc-a")
        rb = _make_report(service="svc-b")
        assert buf.add(ra) == 1
        assert buf.add(rb) == 1

    def test_add_increases_pending_count(self, buf):
        buf.add(_make_report())
        assert buf.pending_count("payment-svc") == 1

    def test_add_does_not_affect_other_service_count(self, buf):
        buf.add(_make_report(service="svc-a"))
        assert buf.pending_count("svc-b") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# DigestBuffer.flush()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestBufferFlush:
    def test_flush_returns_all_buffered_reports(self, buf):
        r1, r2 = _make_report(), _make_report()
        buf.add(r1)
        buf.add(r2)
        flushed = buf.flush("payment-svc")
        assert len(flushed) == 2

    def test_flush_returns_reports_in_insertion_order(self, buf):
        r1 = _make_report(summary="First")
        r2 = _make_report(summary="Second")
        buf.add(r1)
        buf.add(r2)
        flushed = buf.flush("payment-svc")
        assert flushed[0].summary == "First"
        assert flushed[1].summary == "Second"

    def test_flush_clears_buffer_for_service(self, buf):
        buf.add(_make_report())
        buf.flush("payment-svc")
        assert buf.pending_count("payment-svc") == 0

    def test_flush_empty_service_returns_empty_list(self, buf):
        assert buf.flush("nonexistent") == []

    def test_flush_does_not_affect_other_services(self, buf):
        buf.add(_make_report(service="svc-a"))
        buf.add(_make_report(service="svc-b"))
        buf.flush("svc-a")
        assert buf.pending_count("svc-b") == 1

    def test_second_flush_returns_empty(self, buf):
        buf.add(_make_report())
        buf.flush("payment-svc")
        assert buf.flush("payment-svc") == []


# ═══════════════════════════════════════════════════════════════════════════════
# DigestBuffer.flush_all()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestBufferFlushAll:
    def test_flush_all_returns_all_services(self, buf):
        buf.add(_make_report(service="svc-a"))
        buf.add(_make_report(service="svc-b"))
        result = buf.flush_all()
        assert "svc-a" in result
        assert "svc-b" in result

    def test_flush_all_clears_all_buffers(self, buf):
        buf.add(_make_report(service="svc-a"))
        buf.add(_make_report(service="svc-b"))
        buf.flush_all()
        assert buf.total_pending() == 0

    def test_flush_all_on_empty_buffer_returns_empty_dict(self, buf):
        assert buf.flush_all() == {}

    def test_flush_all_report_counts_correct(self, buf):
        for _ in range(3):
            buf.add(_make_report(service="svc-a"))
        for _ in range(2):
            buf.add(_make_report(service="svc-b"))
        result = buf.flush_all()
        assert len(result["svc-a"]) == 3
        assert len(result["svc-b"]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# DigestBuffer.pending_count() / total_pending() / services_with_pending()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestBufferInspection:
    def test_pending_count_zero_for_unknown_service(self, buf):
        assert buf.pending_count("unknown") == 0

    def test_total_pending_sums_all_services(self, buf):
        buf.add(_make_report(service="a"))
        buf.add(_make_report(service="a"))
        buf.add(_make_report(service="b"))
        assert buf.total_pending() == 3

    def test_total_pending_zero_when_empty(self, buf):
        assert buf.total_pending() == 0

    def test_services_with_pending_lists_populated_services(self, buf):
        buf.add(_make_report(service="svc-a"))
        buf.add(_make_report(service="svc-b"))
        pending = buf.services_with_pending()
        assert "svc-a" in pending
        assert "svc-b" in pending

    def test_services_with_pending_excludes_flushed_services(self, buf):
        buf.add(_make_report(service="svc-a"))
        buf.add(_make_report(service="svc-b"))
        buf.flush("svc-a")
        assert "svc-a" not in buf.services_with_pending()
        assert "svc-b" in buf.services_with_pending()

    def test_services_with_pending_empty_when_buffer_clear(self, buf):
        assert buf.services_with_pending() == []


# ═══════════════════════════════════════════════════════════════════════════════
# Thread safety (smoke test)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestBufferThreadSafety:
    def test_concurrent_adds_do_not_raise(self, buf):
        errors: list[Exception] = []

        def work():
            try:
                for _ in range(20):
                    buf.add(_make_report())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_concurrent_add_and_flush_do_not_raise(self, buf):
        errors: list[Exception] = []

        def adder():
            try:
                for _ in range(10):
                    buf.add(_make_report())
            except Exception as exc:
                errors.append(exc)

        def flusher():
            try:
                for _ in range(5):
                    buf.flush("payment-svc")
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=adder) for _ in range(3)] +
            [threading.Thread(target=flusher) for _ in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ═══════════════════════════════════════════════════════════════════════════════
# SlackMessageBuilder.build_digest_message()
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildDigestMessage:
    def _reports(self, n: int = 3, service: str = "payment-svc") -> list[IncidentReport]:
        return [_make_report(service=service, z_score=float(i + 1)) for i in range(n)]

    def test_returns_dict_with_blocks(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        assert "blocks" in payload
        assert isinstance(payload["blocks"], list)

    def test_returns_text_fallback(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        assert "text" in payload
        assert "payment-svc" in payload["text"]

    def test_header_block_present(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        headers = [b for b in payload["blocks"] if b["type"] == "header"]
        assert len(headers) == 1

    def test_header_contains_service_name(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        header_text = payload["blocks"][0]["text"]["text"]
        assert "payment-svc" in header_text

    def test_header_contains_count(self, builder):
        reports = self._reports(3)
        payload = builder.build_digest_message("payment-svc", reports)
        header_text = payload["blocks"][0]["text"]["text"]
        assert "3" in header_text

    def test_header_plural_for_multiple_reports(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports(3))
        header_text = payload["blocks"][0]["text"]["text"]
        assert "alerts" in header_text

    def test_header_singular_for_one_report(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports(1))
        header_text = payload["blocks"][0]["text"]["text"]
        assert "alert" in header_text

    def test_digest_section_mentions_rate_limit(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        texts = [
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
            if b["type"] == "section"
        ]
        assert any("Rate limit" in t or "rate" in t.lower() for t in texts)

    def test_each_report_listed(self, builder):
        reports = self._reports(3)
        payload = builder.build_digest_message("payment-svc", reports)
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
            if b["type"] == "section"
        )
        for r in reports:
            assert r.severity in all_text

    def test_context_block_shows_highest_z_score(self, builder):
        reports = [
            _make_report(z_score=3.0),
            _make_report(z_score=7.5),
            _make_report(z_score=5.0),
        ]
        payload = builder.build_digest_message("payment-svc", reports)
        ctx = next(b for b in payload["blocks"] if b["type"] == "context")
        assert "7.5" in ctx["elements"][0]["text"]

    def test_context_block_shows_avg_confidence(self, builder):
        reports = [
            _make_report(confidence_score=0.8),
            _make_report(confidence_score=0.6),
        ]
        payload = builder.build_digest_message("payment-svc", reports)
        ctx = next(b for b in payload["blocks"] if b["type"] == "context")
        assert "70%" in ctx["elements"][0]["text"]

    def test_context_block_shows_suppressed_count(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports(5))
        ctx = next(b for b in payload["blocks"] if b["type"] == "context")
        assert "5" in ctx["elements"][0]["text"]

    def test_text_fallback_contains_count(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports(4))
        assert "4" in payload["text"]

    def test_no_actions_block_in_digest(self, builder):
        payload = builder.build_digest_message("payment-svc", self._reports())
        assert not any(b["type"] == "actions" for b in payload["blocks"])
