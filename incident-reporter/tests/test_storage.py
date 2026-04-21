"""
Unit tests for Phase 3 Part 4 — ReportStore (OpenSearch persistence).

No real OpenSearch required — OpenSearchClient.with_raw_client() injects a mock.
"""

import json
import uuid
from unittest.mock import MagicMock, call, patch

import pytest

from enrichment.opensearch_client import OpenSearchClient
from report.models import IncidentReport
from storage.report_store import INDEX, ReportStore, _INDEX_MAPPING


# ── shared fixtures ───────────────────────────────────────────────────────────


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
        probable_cause="Misconfigured connection pool in v2.3.1.",
        evidence=["error_rate=0.08", "p99=950ms"],
        suggested_actions=["Roll back to v2.3.0."],
        estimated_user_impact="All checkout transactions failing.",
        confidence_score=0.88,
        ai_generated=True,
        oncall_owner={"team": "payments", "oncall": "pay@company.com",
                      "slack_channel": "#payments-oncall",
                      "pagerduty_service_id": "PD123", "escalation_policy": "pay",
                      "tier": "P1"},
        z_score=6.2,
        observed_metrics={"error_rate": 0.08},
        baseline_metrics={"error_rate": {"ewma": 0.011}},
        pii_scrub_count=2,
    )
    defaults.update(overrides)
    return IncidentReport(**defaults)


@pytest.fixture
def mock_raw():
    raw = MagicMock()
    raw.index.return_value = {"result": "created"}
    raw.indices.exists.return_value = False
    raw.indices.create.return_value = {"acknowledged": True}
    return raw


@pytest.fixture
def store(mock_raw):
    client = OpenSearchClient.with_raw_client(mock_raw)
    return ReportStore(client)


# ═══════════════════════════════════════════════════════════════════════════════
# ReportStore.store()
# ═══════════════════════════════════════════════════════════════════════════════


class TestReportStoreStore:
    def test_calls_index_on_underlying_client(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        mock_raw.index.assert_called_once()

    def test_uses_incident_reports_index(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["index"] == "incident-reports"

    def test_uses_event_id_as_document_id(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["id"] == report.event_id

    def test_document_body_contains_report_id(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["body"]["report_id"] == report.report_id

    def test_document_body_contains_all_required_fields(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        body = kwargs["body"]
        required = {
            "report_id", "event_id", "service", "severity", "anomaly_type",
            "timestamp", "generated_at", "summary", "affected_services",
            "probable_cause", "evidence", "suggested_actions",
            "estimated_user_impact", "confidence_score", "ai_generated",
            "oncall_owner", "z_score", "observed_metrics", "baseline_metrics",
            "pii_scrub_count",
        }
        assert required.issubset(set(body.keys()))

    def test_document_preserves_pii_scrub_count(self, store, mock_raw):
        report = _make_report(pii_scrub_count=7)
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["body"]["pii_scrub_count"] == 7

    def test_document_preserves_ai_generated_flag(self, store, mock_raw):
        report = _make_report(ai_generated=False)
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["body"]["ai_generated"] is False

    def test_document_preserves_confidence_score(self, store, mock_raw):
        report = _make_report(confidence_score=0.72)
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["body"]["confidence_score"] == pytest.approx(0.72)

    def test_returns_true_on_success(self, store, mock_raw):
        mock_raw.index.return_value = {"result": "created"}
        report = _make_report()
        assert store.store(report) is True

    def test_returns_false_on_opensearch_exception(self, store, mock_raw):
        mock_raw.index.side_effect = Exception("cluster unavailable")
        report = _make_report()
        assert store.store(report) is False

    def test_event_id_is_the_link_to_anomaly_event(self, store, mock_raw):
        """The document ID must equal the event_id so the report can be fetched
        by any component that holds a reference to the originating AnomalyEvent."""
        event_id = str(uuid.uuid4())
        report = _make_report(event_id=event_id)
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        assert kwargs["id"] == event_id
        assert kwargs["body"]["event_id"] == event_id

    def test_idempotent_reprocessing_overwrites_same_doc(self, store, mock_raw):
        """Calling store twice with the same event_id must call index twice
        (OpenSearch upsert semantics — same doc ID overwrites the old doc)."""
        report = _make_report()
        store.store(report)
        store.store(report)
        assert mock_raw.index.call_count == 2
        calls = mock_raw.index.call_args_list
        assert calls[0][1]["id"] == calls[1][1]["id"] == report.event_id

    def test_different_events_use_different_doc_ids(self, store, mock_raw):
        r1 = _make_report()
        r2 = _make_report()  # fresh UUIDs
        store.store(r1)
        store.store(r2)
        ids = [c[1]["id"] for c in mock_raw.index.call_args_list]
        assert ids[0] != ids[1]

    def test_document_is_json_serialisable(self, store, mock_raw):
        report = _make_report()
        store.store(report)
        _, kwargs = mock_raw.index.call_args
        json.dumps(kwargs["body"], default=str)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# ReportStore.ensure_index()
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnsureIndex:
    def test_creates_index_when_absent(self, store, mock_raw):
        mock_raw.indices.exists.return_value = False
        store.ensure_index()
        mock_raw.indices.create.assert_called_once()

    def test_create_uses_correct_index_name(self, store, mock_raw):
        mock_raw.indices.exists.return_value = False
        store.ensure_index()
        _, kwargs = mock_raw.indices.create.call_args
        assert kwargs["index"] == "incident-reports"

    def test_create_passes_mapping(self, store, mock_raw):
        mock_raw.indices.exists.return_value = False
        store.ensure_index()
        _, kwargs = mock_raw.indices.create.call_args
        body = kwargs["body"]
        assert "mappings" in body
        assert "settings" in body

    def test_skips_creation_when_index_exists(self, store, mock_raw):
        mock_raw.indices.exists.return_value = True
        store.ensure_index()
        mock_raw.indices.create.assert_not_called()

    def test_checks_correct_index_name_in_exists(self, store, mock_raw):
        mock_raw.indices.exists.return_value = True
        store.ensure_index()
        mock_raw.indices.exists.assert_called_once_with(index="incident-reports")

    def test_graceful_when_exists_raises(self, store, mock_raw):
        mock_raw.indices.exists.side_effect = Exception("connection refused")
        store.ensure_index()  # must not propagate the exception

    def test_graceful_when_create_raises(self, store, mock_raw):
        mock_raw.indices.exists.return_value = False
        mock_raw.indices.create.side_effect = Exception("create failed")
        store.ensure_index()  # must not propagate the exception

    def test_index_mapping_has_keyword_fields_for_aggregations(self):
        props = _INDEX_MAPPING["mappings"]["properties"]
        for field in ("service", "severity", "anomaly_type", "event_id", "report_id"):
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_index_mapping_has_date_fields(self):
        props = _INDEX_MAPPING["mappings"]["properties"]
        assert props["timestamp"]["type"] == "date"
        assert props["generated_at"]["type"] == "date"

    def test_index_mapping_has_float_fields(self):
        props = _INDEX_MAPPING["mappings"]["properties"]
        assert props["confidence_score"]["type"] == "float"
        assert props["z_score"]["type"] == "float"

    def test_index_constant_matches_opensearch_client_constant(self):
        from enrichment.opensearch_client import _INCIDENT_REPORTS_INDEX
        assert INDEX == _INCIDENT_REPORTS_INDEX
