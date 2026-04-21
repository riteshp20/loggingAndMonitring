"""Incident Reporter — Kafka consumer entry point.

Reads AnomalyEvent JSON records from the ``anomaly-events`` topic and drives
each through the full IncidentReportPipeline:
  enrich → PII-scrub → Claude AI report → store in OpenSearch

Environment variables (all have sensible defaults for local dev):
  KAFKA_BROKERS          bootstrap servers            kafka-1:9092,...
  KAFKA_ANOMALY_TOPIC    input topic                  anomaly-events
  KAFKA_CONSUMER_GROUP   consumer group ID            incident-reporter
  OPENSEARCH_URL         OpenSearch URL               http://opensearch:9200
  OPENSEARCH_USERNAME    (optional)
  OPENSEARCH_PASSWORD    (optional)
  SERVICE_REGISTRY_PATH  path to service_registry.json
  ANTHROPIC_API_KEY      Anthropic API key (required for AI reports)
"""

import json
import logging
import os
import signal

from kafka import KafkaConsumer

from enrichment.enricher import EnrichmentService
from enrichment.opensearch_client import OpenSearchClient
from enrichment.registry import ServiceRegistry
from pii.scrubber import PiiScrubber
from pipeline import IncidentReportPipeline
from report.claude_client import ClaudeReportGenerator
from storage.report_store import ReportStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)


def main() -> None:
    kafka_brokers = os.getenv("KAFKA_BROKERS", "kafka-1:9092,kafka-2:9092,kafka-3:9092")
    input_topic = os.getenv("KAFKA_ANOMALY_TOPIC", "anomaly-events")
    consumer_group = os.getenv("KAFKA_CONSUMER_GROUP", "incident-reporter")
    opensearch_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
    opensearch_user = os.getenv("OPENSEARCH_USERNAME", "")
    opensearch_pass = os.getenv("OPENSEARCH_PASSWORD", "")
    registry_path = os.getenv("SERVICE_REGISTRY_PATH", "service_registry.json")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None

    # ── wire components ───────────────────────────────────────────────────
    os_client = OpenSearchClient(
        url=opensearch_url,
        username=opensearch_user,
        password=opensearch_pass,
    )
    registry = ServiceRegistry(registry_path)
    enricher = EnrichmentService(opensearch=os_client, registry=registry)
    scrubber = PiiScrubber()
    generator = ClaudeReportGenerator(
        api_key=anthropic_api_key,
        pii_scrubber=scrubber,
    )
    store = ReportStore(os_client)
    pipeline = IncidentReportPipeline(enricher=enricher, generator=generator, store=store)

    # ── ensure index exists ───────────────────────────────────────────────
    store.ensure_index()

    # ── Kafka consumer ────────────────────────────────────────────────────
    consumer = KafkaConsumer(
        input_topic,
        bootstrap_servers=kafka_brokers.split(","),
        group_id=consumer_group,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )

    logger.info(
        "Incident reporter started | topic=%s group=%s brokers=%s",
        input_topic, consumer_group, kafka_brokers,
    )

    running = True

    def _handle_stop(signum, frame) -> None:  # noqa: ANN001, ARG001
        nonlocal running
        logger.info("Shutdown signal %d received — draining consumer", signum)
        running = False

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    try:
        while running:
            batch = consumer.poll(timeout_ms=1000)
            for _tp, messages in batch.items():
                for msg in messages:
                    if not running:
                        break
                    try:
                        pipeline.process(msg.value)
                    except Exception as exc:
                        logger.error(
                            "Unhandled error at offset=%d partition=%d: %s",
                            msg.offset, msg.partition, exc,
                        )
    finally:
        consumer.close()
        logger.info("Incident reporter stopped")


if __name__ == "__main__":
    main()
