"""
Unit tests for Phase 3 Part 4 — IncidentReportPipeline end-to-end.

All four components (enricher, generator, store) are mocked so no external
services are needed.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from models import EnrichedAnomalyEvent, EnrichedContext
from pipeline import IncidentReportPipeline
from report.models import IncidentReport


# ── shared helpers ────────────────────────────────────────────────────────────

_ONCALL = {
    "team": "payments",
    "oncall": "pay@company.com",
    "slack_channel": "#payments-oncall",
    "pagerduty_service_id": "PD123",
    "escalation_policy": "pay-esc",
    "tier": "P1",
}

_SAMPLE_EVENT_DICT = {
    "event_id": str(uuid.uuid4()),
    "service": "payment-svc",
    "anomaly_type": "zscore_critical",
    "severity": "P1",
    "z_score": 6.2,
    "baseline": {"error_rate": {"ewma": 0.011, "std_dev": 0.002,
                                 "mean": 0.01, "sample_count": 500,
                                 "last_updated": 1714000000.0}},
    "observed": {"error_rate": 0.08},
    "top_log_samples": [{"level": "ERROR", "message": "DB timeout"}],
    "correlated_services": [],
    "timestamp": "2026-04-21T12:00:00+00:00",
}


def _make_enriched(**overrides) -> EnrichedAnomalyEvent:
    base = {**_SAMPLE_EVENT_DICT,
            "log_samples": [{"log_level": "ERROR", "message": "DB timeout"}],
            "correlated_anomalies": [],
            "recent_deployments": [{"service": "payment-svc", "version": "v2.3.1",
                                     "timestamp": "2026-04-21T10:00:00Z",
                                     "deployed_by": "ci"}],
            "oncall_owner": _ONCALL,
            "pii_scrub_count": 0}
    base.update(overrides)
    return EnrichedAnomalyEvent(**base)


def _make_report(**overrides) -> IncidentReport:
    enriched = _make_enriched()
    defaults = dict(
        report_id=str(uuid.uuid4()),
        event_id=enriched.event_id,
        service=enriched.service,
        severity="P1",
        anomaly_type="zscore_critical",
        timestamp=enriched.timestamp,
        generated_at="2026-04-21T12:01:00+00:00",
        summary="DB pool exhausted. Checkout failing.",
        affected_services=["payment-svc"],
        probable_cause="Pool exhaustion from v2.3.1.",
        evidence=["error_rate=0.08"],
        suggested_actions=["Roll back."],
        estimated_user_impact="All checkouts failing.",
        confidence_score=0.88,
        ai_generated=True,
        oncall_owner=_ONCALL,
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=0,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


@pytest.fixture
def mock_enricher():
    m = MagicMock()
    m.enrich.return_value = _make_enriched()
    return m


@pytest.fixture
def mock_generator():
    m = MagicMock()
    m.generate.return_value = _make_report()
    return m


@pytest.fixture
def mock_store():
    m = MagicMock()
    m.store.return_value = True
    return m


@pytest.fixture
def pipeline(mock_enricher, mock_generator, mock_store):
    return IncidentReportPipeline(
        enricher=mock_enricher,
        generator=mock_generator,
        store=mock_store,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineHappyPath:
    def test_returns_incident_report(self, pipeline):
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        assert isinstance(result, IncidentReport)

    def test_calls_enricher_with_raw_event(self, pipeline, mock_enricher):
        pipeline.process(_SAMPLE_EVENT_DICT)
        mock_enricher.enrich.assert_called_once_with(_SAMPLE_EVENT_DICT)

    def test_calls_generator_with_enriched_event(self, pipeline, mock_enricher, mock_generator):
        enriched = _make_enriched()
        mock_enricher.enrich.return_value = enriched
        pipeline.process(_SAMPLE_EVENT_DICT)
        mock_generator.generate.assert_called_once_with(enriched)

    def test_calls_store_with_report(self, pipeline, mock_generator, mock_store):
        report = _make_report()
        mock_generator.generate.return_value = report
        pipeline.process(_SAMPLE_EVENT_DICT)
        mock_store.store.assert_called_once_with(report)

    def test_report_event_id_matches_input_event(self, pipeline, mock_generator):
        expected_event_id = _SAMPLE_EVENT_DICT["event_id"]
        report = _make_report(event_id=expected_event_id)
        mock_generator.generate.return_value = report
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        assert result.event_id == expected_event_id

    def test_report_service_matches_input_event(self, pipeline):
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        assert result.service == "payment-svc"

    def test_stages_called_in_order(self, pipeline, mock_enricher, mock_generator, mock_store):
        """Verify enrich → generate → store ordering via side effects."""
        call_order = []
        mock_enricher.enrich.side_effect = lambda e: (
            call_order.append("enrich"), _make_enriched())[1]
        mock_generator.generate.side_effect = lambda e: (
            call_order.append("generate"), _make_report())[1]
        mock_store.store.side_effect = lambda r: (
            call_order.append("store"), True)[1]
        pipeline.process(_SAMPLE_EVENT_DICT)
        assert call_order == ["enrich", "generate", "store"]

    def test_returns_report_even_when_store_fails(self, pipeline, mock_store):
        mock_store.store.return_value = False
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        # Store failure is non-fatal — report still returned
        assert result is not None
        assert isinstance(result, IncidentReport)

    def test_store_called_even_when_ai_generation_falls_back(
        self, pipeline, mock_generator, mock_store
    ):
        fallback_report = _make_report(ai_generated=False, confidence_score=0.0)
        mock_generator.generate.return_value = fallback_report
        pipeline.process(_SAMPLE_EVENT_DICT)
        mock_store.store.assert_called_once_with(fallback_report)


# ═══════════════════════════════════════════════════════════════════════════════
# Failure modes
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineFailures:
    def test_returns_none_when_enricher_raises(self, pipeline, mock_enricher):
        mock_enricher.enrich.side_effect = RuntimeError("OS down")
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        assert result is None

    def test_returns_none_when_generator_raises(self, pipeline, mock_generator):
        mock_generator.generate.side_effect = RuntimeError("model error")
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        assert result is None

    def test_store_not_called_when_generator_raises(
        self, pipeline, mock_generator, mock_store
    ):
        mock_generator.generate.side_effect = RuntimeError("model error")
        pipeline.process(_SAMPLE_EVENT_DICT)
        mock_store.store.assert_not_called()

    def test_returns_none_on_malformed_event(self, pipeline, mock_enricher):
        """A KeyError from a missing required field should be caught gracefully."""
        mock_enricher.enrich.side_effect = KeyError("event_id")
        result = pipeline.process({})
        assert result is None

    def test_subsequent_events_processed_after_one_failure(
        self, pipeline, mock_enricher, mock_generator
    ):
        """A failed event must not block processing of the next event."""
        mock_enricher.enrich.side_effect = [
            RuntimeError("transient error"),  # first call fails
            _make_enriched(),                  # second call succeeds
        ]
        result1 = pipeline.process(_SAMPLE_EVENT_DICT)
        result2 = pipeline.process(_SAMPLE_EVENT_DICT)
        assert result1 is None
        assert result2 is not None

    def test_returns_none_when_store_raises_unexpectedly(
        self, pipeline, mock_store
    ):
        """Store raising (not just returning False) is also caught gracefully."""
        mock_store.store.side_effect = Exception("disk full")
        result = pipeline.process(_SAMPLE_EVENT_DICT)
        # The report was generated; but store raised, so pipeline catches it.
        # Depending on where the exception is raised, the result may be None.
        # Current pipeline implementation: any exception → None.
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Index constant consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestIndexConstants:
    def test_report_store_index_is_incident_reports(self):
        from storage.report_store import INDEX
        assert INDEX == "incident-reports"

    def test_opensearch_client_incident_reports_constant_matches(self):
        from enrichment.opensearch_client import _INCIDENT_REPORTS_INDEX
        from storage.report_store import INDEX
        assert INDEX == _INCIDENT_REPORTS_INDEX


# ═══════════════════════════════════════════════════════════════════════════════
# Full end-to-end integration (all real components, mocked I/O only)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """Wires real EnrichmentService, PiiScrubber, ClaudeReportGenerator,
    and ReportStore together — only OpenSearch and the Anthropic client are mocked."""

    def _build_pipeline(self) -> tuple[IncidentReportPipeline, MagicMock, MagicMock]:
        import json as _json
        from enrichment.enricher import EnrichmentService
        from enrichment.opensearch_client import OpenSearchClient
        from enrichment.registry import ServiceRegistry
        from pii.scrubber import PiiScrubber
        from report.claude_client import ClaudeReportGenerator
        from storage.report_store import ReportStore

        # Mock OpenSearch raw client
        os_raw = MagicMock()
        os_raw.search.return_value = {"hits": {"hits": []}}
        os_raw.index.return_value = {"result": "created"}
        os_raw.indices.exists.return_value = True

        # Mock Anthropic client
        ai_response_body = _json.dumps({
            "summary": "Payment service spiked. DB timeouts causing checkout failures.",
            "severity": "P1",
            "affected_services": ["payment-svc"],
            "probable_cause": "DB pool exhausted after v2.3.1 deploy.",
            "evidence": ["error_rate=0.08 vs baseline 0.011"],
            "suggested_actions": ["Roll back to v2.3.0.", "Page DBA."],
            "estimated_user_impact": "All checkout transactions failing.",
            "confidence_score": 0.85,
        })
        content_block = MagicMock()
        content_block.text = ai_response_body
        ai_resp = MagicMock()
        ai_resp.content = [content_block]
        mock_anthropic = MagicMock()
        mock_anthropic.messages.create.return_value = ai_resp

        os_client = OpenSearchClient.with_raw_client(os_raw)
        registry = ServiceRegistry.__new__(ServiceRegistry)
        registry._data = {
            "payment-svc": {"team": "payments", "oncall": "pay@co.com",
                            "slack_channel": "#pay", "pagerduty_service_id": "PD1",
                            "escalation_policy": "pay-esc", "tier": "P1"},
            "default": {"team": "platform", "oncall": "oncall@co.com",
                        "slack_channel": "#platform", "pagerduty_service_id": None,
                        "escalation_policy": "default-esc", "tier": "P3"},
        }
        enricher = EnrichmentService(opensearch=os_client, registry=registry)
        scrubber = PiiScrubber()
        generator = ClaudeReportGenerator(_client=mock_anthropic, pii_scrubber=scrubber)
        store = ReportStore(os_client)
        pl = IncidentReportPipeline(enricher=enricher, generator=generator, store=store)
        return pl, os_raw, mock_anthropic

    def test_full_pipeline_returns_incident_report(self):
        pl, _, _ = self._build_pipeline()
        result = pl.process(_SAMPLE_EVENT_DICT)
        assert isinstance(result, IncidentReport)

    def test_full_pipeline_ai_generated_true(self):
        pl, _, _ = self._build_pipeline()
        result = pl.process(_SAMPLE_EVENT_DICT)
        assert result.ai_generated is True

    def test_full_pipeline_event_id_linked(self):
        pl, _, _ = self._build_pipeline()
        result = pl.process(_SAMPLE_EVENT_DICT)
        assert result.event_id == _SAMPLE_EVENT_DICT["event_id"]

    def test_full_pipeline_report_stored_in_opensearch(self):
        pl, os_raw, _ = self._build_pipeline()
        result = pl.process(_SAMPLE_EVENT_DICT)
        # index() called once for the stored report
        os_raw.index.assert_called_once()
        _, kwargs = os_raw.index.call_args
        assert kwargs["index"] == "incident-reports"
        assert kwargs["id"] == _SAMPLE_EVENT_DICT["event_id"]

    def test_full_pipeline_claude_called_with_user_prompt_containing_service(self):
        pl, _, mock_anthropic = self._build_pipeline()
        pl.process(_SAMPLE_EVENT_DICT)
        _, kwargs = mock_anthropic.messages.create.call_args
        user_prompt = kwargs["messages"][0]["content"]
        assert "payment-svc" in user_prompt

    def test_full_pipeline_pii_scrubber_runs_before_claude(self):
        pl, _, mock_anthropic = self._build_pipeline()
        event = {
            **_SAMPLE_EVENT_DICT,
            "top_log_samples": [{"level": "ERROR",
                                  "message": "Auth failed for spy@target.com from 10.0.0.1"}],
        }
        pl.process(event)
        _, kwargs = mock_anthropic.messages.create.call_args
        user_prompt = kwargs["messages"][0]["content"]
        assert "spy@target.com" not in user_prompt
        assert "10.0.0.1" not in user_prompt

    def test_full_pipeline_confidence_in_range(self):
        pl, _, _ = self._build_pipeline()
        result = pl.process(_SAMPLE_EVENT_DICT)
        assert 0.0 <= result.confidence_score <= 1.0

    def test_full_pipeline_fallback_when_claude_down(self):
        pl, _, mock_anthropic = self._build_pipeline()
        mock_anthropic.messages.create.side_effect = Exception("API unavailable")
        result = pl.process(_SAMPLE_EVENT_DICT)
        assert result is not None
        assert result.ai_generated is False
        assert result.confidence_score == 0.0
