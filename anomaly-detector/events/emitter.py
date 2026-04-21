"""
AnomalyEventEmitter — builds AnomalyEvent objects and publishes them to Kafka.

The emitter is decoupled from the Kafka client: it accepts any object with
  .send(topic, key=..., value=...)
  .flush()
  .close()
In production pass a KafkaProducer; in tests pass a MagicMock.

Event construction
──────────────────
build_event()      — single-service anomaly (zscore_critical or both_tiers)
build_storm_event() — system-wide incident when the storm threshold is crossed

Severity mapping
────────────────
  P1  anomaly_type == "system_wide_incident"  OR  z_score > 5.0
  P2  anomaly_type == "both_tiers"
  P3  any other anomaly type (safety fallback)
"""

import json
import uuid
from datetime import datetime, timezone

from detection.models import DetectionResult
from suppression.models import SuppressResult
from .models import AnomalyEvent

TOP_LOG_SAMPLES_LIMIT = 20


def _determine_severity(anomaly_type: str, z_score: float) -> str:
    if anomaly_type == "system_wide_incident" or z_score > 5.0:
        return "P1"
    if anomaly_type == "both_tiers":
        return "P2"
    return "P3"


class AnomalyEventEmitter:
    def __init__(self, producer, topic: str = "anomaly-events") -> None:
        self._producer = producer
        self.topic = topic

    # ── event builders ────────────────────────────────────────────────────────

    def build_event(
        self,
        detection: DetectionResult,
        features: dict,
        baselines: dict,
        top_log_samples: list | None = None,
        correlated_services: list | None = None,
    ) -> AnomalyEvent:
        """
        Build a single-service AnomalyEvent from a DetectionResult.

        Parameters
        ──────────
        detection          Result from TwoTierDetector.detect()
        features           Raw feature vector dict (numeric values become `observed`)
        baselines          {metric: BaselineData asdict} for all tracked metrics
        top_log_samples    Up to 20 log records from the window (optional)
        correlated_services  Services co-alerting in the storm window (optional)
        """
        return AnomalyEvent(
            event_id=str(uuid.uuid4()),
            service=detection.service,
            anomaly_type=detection.verdict_reason,
            severity=_determine_severity(
                detection.verdict_reason, detection.tier1.max_z_score
            ),
            z_score=detection.tier1.max_z_score,
            baseline=baselines,
            observed={
                k: v for k, v in features.items() if isinstance(v, (int, float))
            },
            top_log_samples=(top_log_samples or [])[:TOP_LOG_SAMPLES_LIMIT],
            correlated_services=correlated_services or [],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def build_storm_event(
        self,
        triggering_service: str,
        detection: DetectionResult,
        suppress: SuppressResult,
        top_log_samples: list | None = None,
    ) -> AnomalyEvent:
        """
        Build a system_wide_incident AnomalyEvent when the storm threshold is crossed.

        service is set to "system" to distinguish it from single-service events.
        correlated_services lists every service in the 60-second storm window.
        """
        return AnomalyEvent(
            event_id=str(uuid.uuid4()),
            service="system",
            anomaly_type="system_wide_incident",
            severity="P1",
            z_score=detection.tier1.max_z_score,
            baseline={},
            observed={},
            top_log_samples=(top_log_samples or [])[:TOP_LOG_SAMPLES_LIMIT],
            correlated_services=suppress.storm_services,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ── Kafka emission ────────────────────────────────────────────────────────

    def emit(self, event: AnomalyEvent) -> None:
        """
        Serialise the event to JSON and publish it to the anomaly-events topic.
        The Kafka message key is the service name for partition affinity — all
        events for the same service land on the same partition, preserving order.
        """
        payload = event.to_json().encode("utf-8")
        key = event.service.encode("utf-8")
        self._producer.send(self.topic, key=key, value=payload)

    def flush(self) -> None:
        """Block until all buffered messages are delivered."""
        self._producer.flush()

    def close(self) -> None:
        self._producer.flush()
        self._producer.close()
