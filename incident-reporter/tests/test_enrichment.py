"""
Unit tests for Phase 3 Part 1 — Enrichment pipeline.

No real OpenSearch or Kafka required:
  - OpenSearch client  → unittest.mock.MagicMock via OpenSearchClient.with_raw_client()
  - ServiceRegistry    → tmp_path fixture writes a JSON file
"""

import json
import uuid
from unittest.mock import MagicMock, call

import pytest

from enrichment.opensearch_client import OpenSearchClient, _parse_iso
from enrichment.registry import ServiceRegistry
from enrichment.enricher import EnrichmentService
from models import EnrichedAnomalyEvent, EnrichedContext


# ── shared fixtures ───────────────────────────────────────────────────────────


SAMPLE_EVENT = {
    "event_id": str(uuid.uuid4()),
    "service": "payment-svc",
    "anomaly_type": "zscore_critical",
    "severity": "P1",
    "z_score": 6.2,
    "baseline": {"error_rate": {"mean": 0.01, "std_dev": 0.002, "ewma": 0.011,
                                "sample_count": 500, "last_updated": 1714000000.0}},
    "observed": {"error_rate": 0.08, "p99_latency_ms": 950.0},
    "top_log_samples": [{"level": "ERROR", "message": "DB timeout"}],
    "correlated_services": [],
    "timestamp": "2026-04-21T12:00:00+00:00",
}

REGISTRY_DATA = {
    "payment-svc": {
        "team": "payments",
        "oncall": "pay-oncall@company.com",
        "slack_channel": "#payments-oncall",
        "pagerduty_service_id": "PD123",
        "escalation_policy": "pay-escalation",
        "tier": "P1",
    },
    "default": {
        "team": "platform",
        "oncall": "oncall@company.com",
        "slack_channel": "#platform-oncall",
        "pagerduty_service_id": None,
        "escalation_policy": "default-escalation",
        "tier": "P3",
    },
}


@pytest.fixture
def registry_file(tmp_path):
    p = tmp_path / "service_registry.json"
    p.write_text(json.dumps(REGISTRY_DATA), encoding="utf-8")
    return str(p)


@pytest.fixture
def registry(registry_file):
    return ServiceRegistry(registry_file)


@pytest.fixture
def mock_os_raw():
    """A MagicMock standing in for the underlying opensearch-py client."""
    raw = MagicMock()

    def _fake_search(index, body):
        # Log samples query
        if "service_name.keyword" in str(body):
            return {
                "hits": {
                    "hits": [
                        {"_source": {"@timestamp": "2026-04-21T12:00:00Z",
                                     "message": "DB connection timeout",
                                     "log_level": "ERROR",
                                     "service_name": "payment-svc"}}
                    ]
                }
            }
        # Incident-reports correlation query
        if "incident-reports" in str(index):
            return {
                "hits": {
                    "hits": [
                        {"_source": {"event_id": "corr-001",
                                     "service": "auth-svc",
                                     "anomaly_type": "both_tiers",
                                     "severity": "P2",
                                     "timestamp": "2026-04-21T11:59:00+00:00",
                                     "z_score": 3.8}}
                    ]
                }
            }
        # Deployments query
        if "deployments" in str(index):
            return {
                "hits": {
                    "hits": [
                        {"_source": {"service": "payment-svc",
                                     "version": "v2.3.1",
                                     "timestamp": "2026-04-21T10:00:00Z",
                                     "deployed_by": "ci-pipeline"}}
                    ]
                }
            }
        return {"hits": {"hits": []}}

    raw.search.side_effect = _fake_search
    return raw


@pytest.fixture
def os_client(mock_os_raw):
    return OpenSearchClient.with_raw_client(mock_os_raw)


@pytest.fixture
def enricher(os_client, registry):
    return EnrichmentService(opensearch=os_client, registry=registry)


# ═══════════════════════════════════════════════════════════════════════════════
# ServiceRegistry
# ═══════════════════════════════════════════════════════════════════════════════


class TestServiceRegistry:
    def test_loads_known_service(self, registry):
        owner = registry.get_oncall_owner("payment-svc")
        assert owner is not None
        assert owner["team"] == "payments"
        assert owner["oncall"] == "pay-oncall@company.com"
        assert owner["slack_channel"] == "#payments-oncall"

    def test_falls_back_to_default_for_unknown_service(self, registry):
        owner = registry.get_oncall_owner("unknown-svc")
        assert owner is not None
        assert owner["team"] == "platform"

    def test_returns_none_when_no_default_and_service_unknown(self, tmp_path):
        data = {"auth-svc": {"team": "identity", "oncall": "id@co.com",
                              "slack_channel": "#id", "pagerduty_service_id": None,
                              "escalation_policy": "id-esc", "tier": "P1"}}
        p = tmp_path / "reg.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        reg = ServiceRegistry(str(p))
        assert reg.get_oncall_owner("payment-svc") is None

    def test_graceful_on_missing_file(self):
        reg = ServiceRegistry("/nonexistent/path/registry.json")
        assert reg.get_oncall_owner("payment-svc") is None
        assert reg.all_services() == []

    def test_graceful_on_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{this is not json}", encoding="utf-8")
        reg = ServiceRegistry(str(p))
        assert reg.get_oncall_owner("payment-svc") is None

    def test_all_services_excludes_default(self, registry):
        services = registry.all_services()
        assert "default" not in services
        assert "payment-svc" in services

    def test_all_services_returns_list_of_strings(self, registry):
        services = registry.all_services()
        assert isinstance(services, list)
        assert all(isinstance(s, str) for s in services)

    def test_exact_match_takes_priority_over_default(self, registry):
        owner = registry.get_oncall_owner("payment-svc")
        assert owner["team"] == "payments"  # NOT "platform" (default)

    def test_pagerduty_id_preserved(self, registry):
        owner = registry.get_oncall_owner("payment-svc")
        assert owner["pagerduty_service_id"] == "PD123"

    def test_default_pagerduty_id_can_be_null(self, registry):
        owner = registry.get_oncall_owner("mystery-svc")
        assert owner["pagerduty_service_id"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# OpenSearchClient
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenSearchClientLogSamples:
    def test_returns_list_of_sources(self, os_client, mock_os_raw):
        results = os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00")
        assert isinstance(results, list)
        assert results[0]["log_level"] == "ERROR"

    def test_query_filters_by_service(self, os_client, mock_os_raw):
        os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00")
        _, kwargs = mock_os_raw.search.call_args
        body = kwargs["body"]
        filters = body["query"]["bool"]["filter"]
        service_filter = next(f for f in filters if "term" in f)
        assert service_filter["term"]["service_name.keyword"] == "payment-svc"

    def test_query_includes_time_range(self, os_client, mock_os_raw):
        os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00")
        _, kwargs = mock_os_raw.search.call_args
        body = kwargs["body"]
        filters = body["query"]["bool"]["filter"]
        range_filter = next(f for f in filters if "range" in f)
        assert "@timestamp" in range_filter["range"]

    def test_size_capped_at_limit(self, os_client, mock_os_raw):
        os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00", limit=5)
        _, kwargs = mock_os_raw.search.call_args
        assert kwargs["body"]["size"] == 5

    def test_sorted_by_timestamp_desc(self, os_client, mock_os_raw):
        os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00")
        _, kwargs = mock_os_raw.search.call_args
        sort = kwargs["body"]["sort"]
        assert sort[0]["@timestamp"]["order"] == "desc"

    def test_returns_empty_list_on_exception(self):
        raw = MagicMock()
        raw.search.side_effect = Exception("connection refused")
        client = OpenSearchClient.with_raw_client(raw)
        result = client.fetch_log_samples("svc", "2026-04-21T12:00:00+00:00")
        assert result == []

    def test_uses_logs_wildcard_index(self, os_client, mock_os_raw):
        os_client.fetch_log_samples("payment-svc", "2026-04-21T12:00:00+00:00")
        _, kwargs = mock_os_raw.search.call_args
        assert kwargs["index"] == "logs-*"


class TestOpenSearchClientCorrelation:
    def test_returns_list_of_sources(self, os_client):
        results = os_client.fetch_correlated_anomalies(
            "2026-04-21T12:00:00+00:00", "payment-svc"
        )
        assert isinstance(results, list)

    def test_excludes_the_trigger_service(self, os_client, mock_os_raw):
        # Set up a fresh mock that captures the call
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_correlated_anomalies("2026-04-21T12:00:00+00:00", "payment-svc")
        _, kwargs = raw.search.call_args
        must_not = kwargs["body"]["query"]["bool"]["must_not"]
        assert any("payment-svc" in str(clause) for clause in must_not)

    def test_time_range_is_plus_minus_2_minutes(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_correlated_anomalies("2026-04-21T12:00:00+00:00", "x")
        _, kwargs = raw.search.call_args
        ts_filter = kwargs["body"]["query"]["bool"]["filter"][0]["range"]["timestamp"]
        dt_start = _parse_iso(ts_filter["gte"])
        dt_end = _parse_iso(ts_filter["lte"])
        from datetime import timedelta
        dt_anchor = _parse_iso("2026-04-21T12:00:00+00:00")
        assert abs((dt_anchor - dt_start).total_seconds() - 120) < 1
        assert abs((dt_end - dt_anchor).total_seconds() - 120) < 1

    def test_queries_incident_reports_index(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_correlated_anomalies("2026-04-21T12:00:00+00:00", "x")
        _, kwargs = raw.search.call_args
        assert kwargs["index"] == "incident-reports"

    def test_returns_empty_list_on_exception(self):
        raw = MagicMock()
        raw.search.side_effect = Exception("index not found")
        client = OpenSearchClient.with_raw_client(raw)
        result = client.fetch_correlated_anomalies("2026-04-21T12:00:00+00:00", "x")
        assert result == []


class TestOpenSearchClientDeployments:
    def test_returns_list_of_sources(self, os_client):
        results = os_client.fetch_recent_deployments("payment-svc")
        assert isinstance(results, list)

    def test_query_filters_by_service(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_recent_deployments("payment-svc")
        _, kwargs = raw.search.call_args
        assert kwargs["body"]["query"]["term"]["service.keyword"] == "payment-svc"

    def test_sorted_by_timestamp_desc(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_recent_deployments("payment-svc")
        _, kwargs = raw.search.call_args
        sort = kwargs["body"]["sort"]
        assert sort[0]["timestamp"]["order"] == "desc"

    def test_limit_respected(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_recent_deployments("payment-svc", limit=3)
        _, kwargs = raw.search.call_args
        assert kwargs["body"]["size"] == 3

    def test_returns_empty_list_on_exception(self):
        raw = MagicMock()
        raw.search.side_effect = Exception("timeout")
        client = OpenSearchClient.with_raw_client(raw)
        result = client.fetch_recent_deployments("payment-svc")
        assert result == []

    def test_queries_deployments_index(self, os_client, mock_os_raw):
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        client.fetch_recent_deployments("payment-svc")
        _, kwargs = raw.search.call_args
        assert kwargs["index"] == "deployments"


class TestOpenSearchClientIndexDocument:
    def test_returns_true_on_success(self, os_client, mock_os_raw):
        mock_os_raw.index.return_value = {"result": "created"}
        result = os_client.index_document("incident-reports", "doc-1", {"field": "value"})
        assert result is True

    def test_passes_correct_arguments(self, os_client, mock_os_raw):
        mock_os_raw.index.return_value = {}
        os_client.index_document("incident-reports", "doc-123", {"key": "val"})
        mock_os_raw.index.assert_called_once_with(
            index="incident-reports", id="doc-123", body={"key": "val"}
        )

    def test_returns_false_on_exception(self, os_client, mock_os_raw):
        mock_os_raw.index.side_effect = Exception("cluster unavailable")
        result = os_client.index_document("incident-reports", "doc-1", {})
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# EnrichmentService
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichmentService:
    def test_returns_enriched_anomaly_event(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert isinstance(result, EnrichedAnomalyEvent)

    def test_original_fields_preserved(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert result.event_id == SAMPLE_EVENT["event_id"]
        assert result.service == "payment-svc"
        assert result.severity == "P1"
        assert result.z_score == pytest.approx(6.2)
        assert result.anomaly_type == "zscore_critical"
        assert result.timestamp == SAMPLE_EVENT["timestamp"]

    def test_log_samples_populated(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert len(result.log_samples) > 0
        assert result.log_samples[0]["log_level"] == "ERROR"

    def test_correlated_anomalies_populated(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert isinstance(result.correlated_anomalies, list)

    def test_recent_deployments_populated(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert isinstance(result.recent_deployments, list)

    def test_oncall_owner_populated_for_known_service(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert result.oncall_owner is not None
        assert result.oncall_owner["team"] == "payments"

    def test_oncall_falls_back_to_default_for_unknown_service(self, enricher):
        event = {**SAMPLE_EVENT, "service": "mystery-svc"}
        result = enricher.enrich(event)
        assert result.oncall_owner is not None
        assert result.oncall_owner["team"] == "platform"

    def test_graceful_degradation_all_opensearch_calls_fail(self, registry):
        raw = MagicMock()
        raw.search.side_effect = Exception("OpenSearch unavailable")
        client = OpenSearchClient.with_raw_client(raw)
        svc = EnrichmentService(opensearch=client, registry=registry)
        result = svc.enrich(SAMPLE_EVENT)
        # Should still return a valid event, with empty enrichment fields
        assert isinstance(result, EnrichedAnomalyEvent)
        assert result.log_samples == []
        assert result.correlated_anomalies == []
        assert result.recent_deployments == []

    def test_partial_failure_does_not_prevent_other_fields(self, registry):
        """Deployments failing must not stop log_samples from being returned."""
        raw = MagicMock()

        def selective_fail(index, body):
            if "deployments" in str(index):
                raise Exception("deployments index down")
            return {"hits": {"hits": [{"_source": {"message": "ok"}}]}}

        raw.search.side_effect = selective_fail
        client = OpenSearchClient.with_raw_client(raw)
        svc = EnrichmentService(opensearch=client, registry=registry)
        result = svc.enrich(SAMPLE_EVENT)
        assert len(result.log_samples) > 0
        assert result.recent_deployments == []

    def test_oncall_none_when_registry_empty_and_no_default(self, tmp_path):
        p = tmp_path / "empty_reg.json"
        p.write_text("{}", encoding="utf-8")
        reg = ServiceRegistry(str(p))
        raw = MagicMock()
        raw.search.return_value = {"hits": {"hits": []}}
        client = OpenSearchClient.with_raw_client(raw)
        svc = EnrichmentService(opensearch=client, registry=reg)
        result = svc.enrich(SAMPLE_EVENT)
        assert result.oncall_owner is None

    def test_top_log_samples_from_original_event_preserved(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert result.top_log_samples == SAMPLE_EVENT["top_log_samples"]

    def test_correlated_services_from_original_event_preserved(self, enricher):
        event = {**SAMPLE_EVENT, "correlated_services": ["auth-svc", "order-svc"]}
        result = enricher.enrich(event)
        assert result.correlated_services == ["auth-svc", "order-svc"]

    def test_pii_scrub_count_defaults_to_zero(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        assert result.pii_scrub_count == 0

    def test_to_dict_returns_all_fields(self, enricher):
        result = enricher.enrich(SAMPLE_EVENT)
        d = result.to_dict()
        required = {
            "event_id", "service", "anomaly_type", "severity", "z_score",
            "baseline", "observed", "top_log_samples", "correlated_services",
            "timestamp", "log_samples", "correlated_anomalies",
            "recent_deployments", "oncall_owner", "pii_scrub_count",
        }
        assert required == set(d.keys())

    def test_to_json_is_valid_json(self, enricher):
        import json as _json
        result = enricher.enrich(SAMPLE_EVENT)
        parsed = _json.loads(result.to_json())
        assert parsed["service"] == "payment-svc"
        assert parsed["severity"] == "P1"


# ═══════════════════════════════════════════════════════════════════════════════
# EnrichedAnomalyEvent model
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichedAnomalyEventModel:
    def _make(self, **overrides) -> EnrichedAnomalyEvent:
        ctx = EnrichedContext(
            log_samples=[{"msg": "e"}],
            correlated_anomalies=[],
            recent_deployments=[{"version": "v1"}],
            oncall_owner={"team": "payments"},
        )
        event = {**SAMPLE_EVENT, **overrides}
        return EnrichedAnomalyEvent.from_event_and_context(event, ctx)

    def test_from_event_and_context_maps_all_fields(self):
        enriched = self._make()
        assert enriched.event_id == SAMPLE_EVENT["event_id"]
        assert enriched.log_samples == [{"msg": "e"}]
        assert enriched.recent_deployments == [{"version": "v1"}]
        assert enriched.oncall_owner == {"team": "payments"}

    def test_z_score_cast_to_float(self):
        enriched = self._make(z_score="4.5")  # string input
        assert isinstance(enriched.z_score, float)
        assert enriched.z_score == pytest.approx(4.5)

    def test_missing_optional_fields_default_to_empty(self):
        event = {
            "event_id": "x", "service": "svc", "anomaly_type": "both_tiers",
            "severity": "P2", "z_score": 3.1, "timestamp": "2026-04-21T00:00:00+00:00",
        }
        ctx = EnrichedContext()
        enriched = EnrichedAnomalyEvent.from_event_and_context(event, ctx)
        assert enriched.baseline == {}
        assert enriched.observed == {}
        assert enriched.top_log_samples == []
        assert enriched.correlated_services == []
