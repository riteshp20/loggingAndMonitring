"""
Prometheus metrics registry for the anomaly-detector service.

Metrics exposed
───────────────
Pipeline
  events_ingested_total            Counter   — records pulled from processed-logs
  processing_latency_seconds       Histogram — full pipeline wall-clock time per record
  kafka_consumer_lag               Gauge     — current consumer-group lag (set externally)

Detection
  anomalies_detected_total         Counter   — anomalies that passed all suppression
  model_inference_latency_seconds  Histogram — time spent inside TwoTierDetector.detect()

A Prometheus HTTP server is started on METRICS_PORT (default 9100) so Prometheus
can scrape /metrics without any extra dependency on the application's Kafka consumer.
"""

from __future__ import annotations

import logging
import os
import threading

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CollectorRegistry,
    start_http_server,
    REGISTRY,
)

logger = logging.getLogger(__name__)

# ── metric definitions ────────────────────────────────────────────────────────

EVENTS_INGESTED = Counter(
    "events_ingested_total",
    "Total ServiceWindowAggregate records consumed from processed-logs",
    ["service"],
)

PROCESSING_LATENCY = Histogram(
    "processing_latency_seconds",
    "End-to-end pipeline latency per record (baseline → detection → suppression → emit)",
    ["service"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

KAFKA_CONSUMER_LAG = Gauge(
    "kafka_consumer_lag",
    "Current Kafka consumer-group lag in number of messages",
    ["topic", "partition"],
)

ANOMALIES_DETECTED = Counter(
    "anomalies_detected_total",
    "Anomaly events emitted to anomaly-events topic after passing suppression",
    ["service", "verdict_reason", "severity"],
)

MODEL_INFERENCE_LATENCY = Histogram(
    "model_inference_latency_seconds",
    "Time spent inside TwoTierDetector.detect() (both tiers combined)",
    ["service"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

SUPPRESSED_ALERTS = Counter(
    "suppressed_alerts_total",
    "Anomalies suppressed before emitting (cooldown or storm)",
    ["service", "reason"],
)

# ── server lifecycle ──────────────────────────────────────────────────────────

_server_started = False
_lock = threading.Lock()


def start_metrics_server(port: int | None = None) -> None:
    """Start the Prometheus HTTP server (idempotent — safe to call multiple times)."""
    global _server_started
    with _lock:
        if _server_started:
            return
        port = port or int(os.getenv("METRICS_PORT", "9100"))
        start_http_server(port)
        _server_started = True
        logger.info("Prometheus metrics server started on port %d", port)
