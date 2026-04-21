"""IncidentReportPipeline — wires all four Phase 3 stages into one call."""

from __future__ import annotations

import logging
from typing import Optional

from enrichment.enricher import EnrichmentService
from report.claude_client import ClaudeReportGenerator
from report.models import IncidentReport
from storage.report_store import ReportStore

logger = logging.getLogger(__name__)


class IncidentReportPipeline:
    """Single entry point that drives the full incident-reporting flow.

    Stage order:
      1. Enrich   — fetch log samples, correlated anomalies, deployments, on-call
      2. Scrub    — PII redaction (inside ClaudeReportGenerator if scrubber wired)
      3. Generate — call Claude API, fallback to raw metrics on API failure
      4. Store    — persist IncidentReport in OpenSearch ``incident-reports`` index
    """

    def __init__(
        self,
        enricher: EnrichmentService,
        generator: ClaudeReportGenerator,
        store: ReportStore,
    ) -> None:
        self._enricher = enricher
        self._generator = generator
        self._store = store

    def process(self, raw_event: dict) -> Optional[IncidentReport]:
        """Process one AnomalyEvent dict end-to-end.

        Returns the ``IncidentReport`` on success (even if persistence fails),
        or ``None`` when the event is malformed or an unexpected error occurs.
        A store failure does not suppress the return value — the report has
        already been generated and should not be silently discarded.
        """
        event_id = raw_event.get("event_id", "unknown")
        service = raw_event.get("service", "unknown")

        try:
            # Stage 1: Enrichment
            enriched = self._enricher.enrich(raw_event)

            # Stage 2+3: PII scrub (if wired) + AI report generation
            report = self._generator.generate(enriched)

            # Stage 4: Persistence — failure is non-fatal for the caller
            stored = self._store.store(report)
            if not stored:
                logger.warning(
                    "Report generated but not persisted | "
                    "report_id=%s event_id=%s",
                    report.report_id, event_id,
                )

            logger.info(
                "Pipeline complete | event_id=%s service=%s report_id=%s "
                "severity=%s ai=%s pii_scrubbed=%d stored=%s",
                event_id, service, report.report_id,
                report.severity, report.ai_generated,
                report.pii_scrub_count, stored,
            )
            return report

        except Exception as exc:
            logger.error(
                "Pipeline failed | event_id=%s service=%s error=%s",
                event_id, service, exc,
            )
            return None
