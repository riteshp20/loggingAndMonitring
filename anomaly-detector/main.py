"""
Anomaly Detector — entry point.

Consumes ServiceWindowAggregate records from processed-logs,
runs the four-phase pipeline, and publishes AnomalyEvent records
to the anomaly-events Kafka topic.

Environment variables
─────────────────────
  KAFKA_BROKERS        Comma-separated list  (default: kafka-1:9092,kafka-2:9092,kafka-3:9092)
  KAFKA_INPUT_TOPIC    Source topic          (default: processed-logs)
  KAFKA_ANOMALY_TOPIC  Sink topic            (default: anomaly-events)
  KAFKA_CONSUMER_GROUP Consumer group ID     (default: anomaly-detector)
  REDIS_URL            Redis connection URL  (default: redis://redis:6379)
"""

import json
import logging
import os
import signal
import sys

import redis
from kafka import KafkaConsumer, KafkaProducer

from baseline.manager import BaselineManager
from detection.detector import TwoTierDetector
from detection.tier1_zscore import ZScoreDetector
from detection.tier2_isolation import IsolationForestDetector
from suppression.suppressor import AlertSuppressor
from events.emitter import AnomalyEventEmitter
from pipeline import AnomalyDetectionPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger("anomaly-detector")

_SHUTDOWN = False


def _handle_sigterm(signum, frame):
    global _SHUTDOWN
    log.info("Received signal %s — shutting down", signum)
    _SHUTDOWN = True


def _extract_features(record: dict) -> dict:
    """Map ServiceWindowAggregate JSON fields to the detector's feature key names."""
    return {
        "error_rate":     float(record.get("errorRate", 0.0)),
        "p99_latency_ms": float(record.get("p99LatencyMs", 0.0)),
        "avg_latency_ms": float(record.get("avgLatencyMs", 0.0)),
        "request_volume": float(record.get("requestVolume", 0)),
        "warn_rate":      float(record.get("warnRate", 0.0)),
    }


def _build_pipeline() -> AnomalyDetectionPipeline:
    redis_url    = os.getenv("REDIS_URL",    "redis://redis:6379")
    kafka_brokers = os.getenv("KAFKA_BROKERS", "kafka-1:9092,kafka-2:9092,kafka-3:9092")
    output_topic  = os.getenv("KAFKA_ANOMALY_TOPIC", "anomaly-events")

    r = redis.Redis.from_url(redis_url, decode_responses=False)

    baseline_mgr = BaselineManager(r)
    t1           = ZScoreDetector(baseline_mgr)
    t2           = IsolationForestDetector(r)
    detector     = TwoTierDetector(baseline_mgr, t1, t2)
    suppressor   = AlertSuppressor(r)

    producer = KafkaProducer(bootstrap_servers=kafka_brokers.split(","))
    emitter  = AnomalyEventEmitter(producer, topic=output_topic)

    return AnomalyDetectionPipeline(baseline_mgr, detector, suppressor, emitter)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT,  _handle_sigterm)

    kafka_brokers  = os.getenv("KAFKA_BROKERS",  "kafka-1:9092,kafka-2:9092,kafka-3:9092")
    input_topic    = os.getenv("KAFKA_INPUT_TOPIC",    "processed-logs")
    consumer_group = os.getenv("KAFKA_CONSUMER_GROUP", "anomaly-detector")

    pipeline = _build_pipeline()
    log.info("Pipeline ready — consuming from %s", input_topic)

    consumer = KafkaConsumer(
        input_topic,
        bootstrap_servers=kafka_brokers.split(","),
        group_id=consumer_group,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )

    try:
        for msg in consumer:
            if _SHUTDOWN:
                break
            record = msg.value
            if record.get("RECORD_TYPE") != "SERVICE_WINDOW_AGGREGATE":
                continue
            service  = record.get("service", "unknown")
            features = _extract_features(record)
            top_logs = record.get("topErrors", [])

            try:
                event = pipeline.process_window(service, features, top_logs)
                if event:
                    log.info(
                        "EMITTED %s %-20s service=%-30s z=%.2f",
                        event.severity, event.anomaly_type, event.service, event.z_score,
                    )
            except Exception:
                log.exception("Error processing window for service=%s", service)
    finally:
        consumer.close()
        pipeline.emitter.close()
        log.info("Shut down cleanly")


if __name__ == "__main__":
    main()
