"""
Unit tests for AnomalyEventEmitter, AnomalyEvent, and AnomalyDetectionPipeline.

No real Kafka or Redis required:
  - Kafka producer → unittest.mock.MagicMock
  - Redis → fakeredis.FakeRedis
"""

import json
import time
import uuid
from dataclasses import asdict
from unittest.mock import MagicMock, call

import fakeredis
import pytest

from baseline.manager import BaselineManager
from detection.detector import TwoTierDetector, ZSCORE_CRITICAL_THRESHOLD
from detection.models import DetectionResult, ZScoreResult, IsolationForestResult
from detection.tier1_zscore import ZScoreDetector
from detection.tier2_isolation import IsolationForestDetector
from events.emitter import AnomalyEventEmitter, _determine_severity
from events.models import AnomalyEvent
from pipeline import AnomalyDetectionPipeline
from suppression.models import SuppressResult
from suppression.suppressor import AlertSuppressor


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_detection(
    service: str = "svc-a",
    is_anomaly: bool = True,
    verdict: str = "both_tiers",
    max_z: float = 4.0,
    tier2_fired: bool = True,
    learning: bool = False,
) -> DetectionResult:
    return DetectionResult(
        service=service,
        timestamp=time.time(),
        tier1=ZScoreResult(
            fired=(max_z > 3.0),
            max_z_score=max_z,
            z_scores={"error_rate": max_z},
        ),
        tier2=IsolationForestResult(
            fired=tier2_fired,
            anomaly_score=-0.25,
            model_available=True,
        ),
        is_anomaly=is_anomaly,
        verdict_reason=verdict,
        learning=learning,
    )


def _make_suppress(
    allowed: bool = True,
    reason: str = "allowed",
    is_storm: bool = False,
    storm_services: list | None = None,
    count: int = 1,
) -> SuppressResult:
    return SuppressResult(
        allowed=allowed,
        reason=reason,
        is_storm=is_storm,
        storm_service_count=count,
        storm_services=storm_services or ["svc-a"],
    )


@pytest.fixture
def mock_producer():
    return MagicMock()


@pytest.fixture
def emitter(mock_producer):
    return AnomalyEventEmitter(mock_producer, topic="anomaly-events")


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def pipeline(r, mock_producer):
    mgr  = BaselineManager(r)
    t1   = ZScoreDetector(mgr)
    t2   = IsolationForestDetector(r, min_training_samples=20)
    det  = TwoTierDetector(mgr, t1, t2)
    sup  = AlertSuppressor(r)
    emit = AnomalyEventEmitter(mock_producer, topic="anomaly-events")
    return AnomalyDetectionPipeline(mgr, det, sup, emit)


FEATURES = {
    "error_rate": 0.03,
    "p99_latency_ms": 250.0,
    "avg_latency_ms": 110.0,
    "request_volume": 800.0,
    "warn_rate": 0.05,
}


# ═══════════════════════════════════════════════════════════════════════════════
# AnomalyEvent model
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnomalyEventModel:
    def test_to_dict_contains_all_fields(self):
        event = AnomalyEvent(
            event_id="abc",
            service="svc",
            anomaly_type="both_tiers",
            severity="P2",
            z_score=4.0,
            baseline={"error_rate": {"ewma": 0.01}},
            observed={"error_rate": 0.03},
            top_log_samples=[{"msg": "error"}],
            correlated_services=[],
            timestamp="2026-04-21T00:00:00+00:00",
        )
        d = event.to_dict()
        required_keys = {
            "event_id", "service", "anomaly_type", "severity",
            "z_score", "baseline", "observed", "top_log_samples",
            "correlated_services", "timestamp",
        }
        assert required_keys == set(d.keys())

    def test_to_json_is_valid_json(self):
        event = AnomalyEvent(
            event_id="abc", service="svc", anomaly_type="both_tiers",
            severity="P2", z_score=4.0, baseline={}, observed={},
            top_log_samples=[], correlated_services=[],
            timestamp="2026-04-21T00:00:00+00:00",
        )
        parsed = json.loads(event.to_json())
        assert parsed["service"] == "svc"
        assert parsed["z_score"] == 4.0

    def test_to_json_round_trips(self):
        event = AnomalyEvent(
            event_id="xyz", service="payment-svc",
            anomaly_type="zscore_critical", severity="P1",
            z_score=7.5, baseline={"latency": {"ewma": 200.0}},
            observed={"latency": 800.0},
            top_log_samples=[{"level": "ERROR", "msg": "timeout"}],
            correlated_services=["svc-b"],
            timestamp="2026-04-21T00:00:00+00:00",
        )
        restored = json.loads(event.to_json())
        assert restored["event_id"] == "xyz"
        assert restored["top_log_samples"][0]["level"] == "ERROR"
        assert restored["correlated_services"] == ["svc-b"]


# ═══════════════════════════════════════════════════════════════════════════════
# Severity determination
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeverityDetermination:
    def test_system_wide_incident_is_p1(self):
        assert _determine_severity("system_wide_incident", 0.0) == "P1"

    def test_zscore_critical_is_p1(self):
        assert _determine_severity("zscore_critical", 6.0) == "P1"

    def test_zscore_above_5_is_p1_regardless_of_type(self):
        assert _determine_severity("both_tiers", 5.1) == "P1"

    def test_both_tiers_with_z_below_5_is_p2(self):
        assert _determine_severity("both_tiers", 4.0) == "P2"

    def test_unknown_type_is_p3(self):
        assert _determine_severity("unknown", 3.5) == "P3"


# ═══════════════════════════════════════════════════════════════════════════════
# AnomalyEventEmitter — event building
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildEvent:
    def test_event_id_is_uuid(self, emitter):
        detection = _make_detection()
        event = emitter.build_event(detection, FEATURES, {})
        assert uuid.UUID(event.event_id)   # raises ValueError if not a valid UUID

    def test_service_matches_detection(self, emitter):
        detection = _make_detection(service="payment-svc")
        event = emitter.build_event(detection, FEATURES, {})
        assert event.service == "payment-svc"

    def test_anomaly_type_matches_verdict(self, emitter):
        detection = _make_detection(verdict="both_tiers")
        event = emitter.build_event(detection, FEATURES, {})
        assert event.anomaly_type == "both_tiers"

    def test_z_score_matches_tier1_max(self, emitter):
        detection = _make_detection(max_z=4.5)
        event = emitter.build_event(detection, FEATURES, {})
        assert event.z_score == pytest.approx(4.5, abs=1e-9)

    def test_severity_p1_for_zscore_critical(self, emitter):
        detection = _make_detection(verdict="zscore_critical", max_z=6.0)
        event = emitter.build_event(detection, FEATURES, {})
        assert event.severity == "P1"

    def test_severity_p2_for_both_tiers(self, emitter):
        detection = _make_detection(verdict="both_tiers", max_z=4.0)
        event = emitter.build_event(detection, FEATURES, {})
        assert event.severity == "P2"

    def test_observed_contains_numeric_features_only(self, emitter):
        detection = _make_detection()
        features_mixed = {**FEATURES, "service_name": "my-svc", "env": "prod"}
        event = emitter.build_event(detection, features_mixed, {})
        assert "service_name" not in event.observed
        assert "env" not in event.observed
        assert "error_rate" in event.observed

    def test_baseline_passed_through(self, emitter):
        detection = _make_detection()
        baselines = {"error_rate": {"ewma": 0.01, "std_dev": 0.002}}
        event = emitter.build_event(detection, FEATURES, baselines)
        assert event.baseline["error_rate"]["ewma"] == 0.01

    def test_top_log_samples_capped_at_20(self, emitter):
        detection = _make_detection()
        samples = [{"msg": f"log-{i}"} for i in range(30)]
        event = emitter.build_event(detection, FEATURES, {}, top_log_samples=samples)
        assert len(event.top_log_samples) == 20

    def test_top_log_samples_default_empty_list(self, emitter):
        detection = _make_detection()
        event = emitter.build_event(detection, FEATURES, {})
        assert event.top_log_samples == []

    def test_correlated_services_passed_through(self, emitter):
        detection = _make_detection()
        event = emitter.build_event(
            detection, FEATURES, {}, correlated_services=["svc-b", "svc-c"]
        )
        assert event.correlated_services == ["svc-b", "svc-c"]

    def test_timestamp_is_iso8601_utc(self, emitter):
        detection = _make_detection()
        event = emitter.build_event(detection, FEATURES, {})
        assert "T" in event.timestamp
        assert "+00:00" in event.timestamp or event.timestamp.endswith("Z")


class TestBuildStormEvent:
    def test_service_is_system(self, emitter):
        detection = _make_detection()
        suppress = _make_suppress(is_storm=True, storm_services=["a", "b", "c", "d", "e", "f"])
        event = emitter.build_storm_event("svc-a", detection, suppress)
        assert event.service == "system"

    def test_anomaly_type_is_system_wide_incident(self, emitter):
        detection = _make_detection()
        suppress = _make_suppress(is_storm=True, storm_services=["a", "b", "c", "d", "e", "f"])
        event = emitter.build_storm_event("svc-a", detection, suppress)
        assert event.anomaly_type == "system_wide_incident"

    def test_severity_is_always_p1(self, emitter):
        detection = _make_detection(max_z=0.5)  # low z
        suppress = _make_suppress(is_storm=True, storm_services=["a", "b", "c", "d", "e", "f"])
        event = emitter.build_storm_event("svc-a", detection, suppress)
        assert event.severity == "P1"

    def test_correlated_services_from_suppress_result(self, emitter):
        storm_svcs = ["a", "b", "c", "d", "e", "f"]
        detection = _make_detection()
        suppress = _make_suppress(is_storm=True, storm_services=storm_svcs)
        event = emitter.build_storm_event("svc-a", detection, suppress)
        assert event.correlated_services == storm_svcs

    def test_observed_and_baseline_are_empty(self, emitter):
        detection = _make_detection()
        suppress = _make_suppress(is_storm=True, storm_services=["a", "b", "c", "d", "e", "f"])
        event = emitter.build_storm_event("svc-a", detection, suppress)
        assert event.observed == {}
        assert event.baseline == {}

    def test_top_log_samples_capped_at_20(self, emitter):
        detection = _make_detection()
        suppress = _make_suppress(is_storm=True, storm_services=["a", "b", "c", "d", "e", "f"])
        samples = [{"msg": f"e{i}"} for i in range(25)]
        event = emitter.build_storm_event("svc-a", detection, suppress, samples)
        assert len(event.top_log_samples) == 20


# ═══════════════════════════════════════════════════════════════════════════════
# AnomalyEventEmitter — Kafka emission
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmitToKafka:
    def _sample_event(self) -> AnomalyEvent:
        return AnomalyEvent(
            event_id="test-id", service="svc-a",
            anomaly_type="both_tiers", severity="P2",
            z_score=4.0, baseline={}, observed={"error_rate": 0.03},
            top_log_samples=[], correlated_services=[],
            timestamp="2026-04-21T00:00:00+00:00",
        )

    def test_emit_calls_producer_send(self, emitter, mock_producer):
        emitter.emit(self._sample_event())
        mock_producer.send.assert_called_once()

    def test_emit_uses_correct_topic(self, emitter, mock_producer):
        emitter.emit(self._sample_event())
        args, kwargs = mock_producer.send.call_args
        assert args[0] == "anomaly-events"

    def test_emit_key_is_service_name_bytes(self, emitter, mock_producer):
        emitter.emit(self._sample_event())
        _, kwargs = mock_producer.send.call_args
        assert kwargs["key"] == b"svc-a"

    def test_emit_value_is_valid_json_bytes(self, emitter, mock_producer):
        emitter.emit(self._sample_event())
        _, kwargs = mock_producer.send.call_args
        parsed = json.loads(kwargs["value"].decode("utf-8"))
        assert parsed["service"] == "svc-a"
        assert parsed["severity"] == "P2"

    def test_emit_value_contains_all_schema_fields(self, emitter, mock_producer):
        emitter.emit(self._sample_event())
        _, kwargs = mock_producer.send.call_args
        parsed = json.loads(kwargs["value"].decode("utf-8"))
        for field in ["event_id", "service", "anomaly_type", "severity",
                      "z_score", "baseline", "observed", "top_log_samples",
                      "correlated_services", "timestamp"]:
            assert field in parsed, f"missing field: {field}"

    def test_flush_delegates_to_producer(self, emitter, mock_producer):
        emitter.flush()
        mock_producer.flush.assert_called_once()

    def test_close_flushes_then_closes(self, emitter, mock_producer):
        emitter.close()
        mock_producer.flush.assert_called_once()
        mock_producer.close.assert_called_once()

    def test_storm_event_key_is_system_bytes(self, mock_producer):
        emitter = AnomalyEventEmitter(mock_producer, topic="anomaly-events")
        event = AnomalyEvent(
            event_id="s-id", service="system",
            anomaly_type="system_wide_incident", severity="P1",
            z_score=0.0, baseline={}, observed={},
            top_log_samples=[], correlated_services=["a", "b"],
            timestamp="2026-04-21T00:00:00+00:00",
        )
        emitter.emit(event)
        _, kwargs = mock_producer.send.call_args
        assert kwargs["key"] == b"system"


# ═══════════════════════════════════════════════════════════════════════════════
# AnomalyDetectionPipeline — end-to-end
# ═══════════════════════════════════════════════════════════════════════════════


def _seed_baseline(r, service, metric, ewma, std_dev):
    r.hset(f"svc:{service}:baseline:{metric}", mapping={
        "mean": ewma, "std_dev": std_dev, "ewma": ewma,
        "sample_count": 100, "last_updated": time.time(),
    })
    r.set(f"svc:{service}:first_seen", time.time() - 25 * 3_600)


class TestPipeline:
    def test_no_anomaly_returns_none(self, pipeline, r):
        # Seed very loose baselines so incoming values don't fire
        _seed_baseline(r, "svc-p1", "error_rate", ewma=0.01, std_dev=5.0)
        result = pipeline.process_window("svc-p1", {"error_rate": 0.01})
        assert result is None

    def test_anomaly_emits_event_and_returns_it(self, pipeline, r, mock_producer):
        # Tight baseline → z > 3 → tier1 fires; no IF model → only zscore_critical fires
        _seed_baseline(r, "svc-p2", "error_rate", ewma=0.01, std_dev=0.001)
        # value gives z = (0.02 - 0.01) / 0.001 = 10 → zscore_critical
        event = pipeline.process_window("svc-p2", {"error_rate": 0.02})
        assert event is not None
        assert event.service == "svc-p2"
        assert event.severity == "P1"
        mock_producer.send.assert_called_once()

    def test_second_call_same_service_suppressed_by_cooldown(self, pipeline, r, mock_producer):
        _seed_baseline(r, "svc-p3", "error_rate", ewma=0.01, std_dev=0.001)
        pipeline.process_window("svc-p3", {"error_rate": 0.02})  # fires
        mock_producer.reset_mock()
        result = pipeline.process_window("svc-p3", {"error_rate": 0.02})  # cooldown
        assert result is None
        mock_producer.send.assert_not_called()

    def test_storm_triggers_system_wide_event(self, pipeline, r, mock_producer):
        """6 services with critical z-scores should produce a system_wide_incident."""
        for i in range(6):
            _seed_baseline(r, f"storm-svc-{i}", "error_rate", ewma=0.01, std_dev=0.001)

        events = []
        for i in range(6):
            e = pipeline.process_window(f"storm-svc-{i}", {"error_rate": 0.02})
            if e:
                events.append(e)

        storm_events = [e for e in events if e.anomaly_type == "system_wide_incident"]
        assert len(storm_events) == 1
        assert storm_events[0].severity == "P1"
        assert storm_events[0].service == "system"
        assert len(storm_events[0].correlated_services) == 6

    def test_event_baseline_populated_for_tracked_metrics(self, pipeline, r):
        _seed_baseline(r, "svc-p5", "error_rate", ewma=0.01, std_dev=0.001)
        event = pipeline.process_window("svc-p5", {"error_rate": 0.02})
        if event:
            assert "error_rate" in event.baseline
            assert "ewma" in event.baseline["error_rate"]

    def test_top_log_samples_passed_to_event(self, pipeline, r):
        _seed_baseline(r, "svc-p6", "error_rate", ewma=0.01, std_dev=0.001)
        logs = [{"level": "ERROR", "msg": f"err-{i}"} for i in range(5)]
        event = pipeline.process_window("svc-p6", {"error_rate": 0.02}, top_log_samples=logs)
        if event:
            assert len(event.top_log_samples) == 5

    def test_learning_service_never_emits(self, pipeline, r, mock_producer):
        """Services in cold-start window must not trigger any emission."""
        # Do NOT set first_seen → service is learning (no key set)
        # Force high z-score: set baseline but NOT first_seen
        r.hset("svc:svc-learn:baseline:error_rate", mapping={
            "mean": 0.01, "std_dev": 0.001, "ewma": 0.01,
            "sample_count": 100, "last_updated": time.time(),
        })
        # first_seen not set → is_learning() returns True → no anomaly emitted
        result = pipeline.process_window("svc-learn", {"error_rate": 0.99})
        assert result is None
        mock_producer.send.assert_not_called()
