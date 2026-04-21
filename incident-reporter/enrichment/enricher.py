"""EnrichmentService — parallel fetch of all context for an AnomalyEvent."""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from .opensearch_client import OpenSearchClient
from .registry import ServiceRegistry
from models import EnrichedAnomalyEvent, EnrichedContext

logger = logging.getLogger(__name__)


class EnrichmentService:
    """Orchestrates three parallel OpenSearch fetches plus a registry lookup."""

    def __init__(
        self,
        opensearch: OpenSearchClient,
        registry: ServiceRegistry,
    ) -> None:
        self._os = opensearch
        self._registry = registry

    def enrich(self, event: dict) -> EnrichedAnomalyEvent:
        """Enrich *event* with log samples, correlations, deployments, and on-call info.

        All three OpenSearch calls run concurrently.  Any individual failure
        degrades gracefully — the corresponding field in the result is an empty
        list / None rather than raising.
        """
        service = event.get("service", "")
        timestamp = event.get("timestamp", "")

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_logs: Future = pool.submit(
                self._os.fetch_log_samples, service, timestamp
            )
            f_corr: Future = pool.submit(
                self._os.fetch_correlated_anomalies, timestamp, service
            )
            f_deps: Future = pool.submit(
                self._os.fetch_recent_deployments, service
            )

            log_samples = _safe_result(f_logs, default=[])
            correlated = _safe_result(f_corr, default=[])
            deployments = _safe_result(f_deps, default=[])

        oncall = self._registry.get_oncall_owner(service)

        ctx = EnrichedContext(
            log_samples=log_samples,
            correlated_anomalies=correlated,
            recent_deployments=deployments,
            oncall_owner=oncall,
        )
        enriched = EnrichedAnomalyEvent.from_event_and_context(event, ctx)
        logger.info(
            "Enriched event %s for service=%s: logs=%d correlated=%d deployments=%d oncall=%s",
            enriched.event_id,
            service,
            len(log_samples),
            len(correlated),
            len(deployments),
            oncall is not None,
        )
        return enriched


# ── helpers ───────────────────────────────────────────────────────────────────


def _safe_result(future: Future, default):
    try:
        return future.result()
    except Exception as exc:
        logger.warning("Enrichment future failed: %s", exc)
        return default
