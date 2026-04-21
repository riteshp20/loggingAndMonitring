"""ReportStore — persists IncidentReport documents to OpenSearch.

Document ID is the originating ``event_id`` so every report is:
  - Idempotent: re-processing the same AnomalyEvent overwrites the same doc.
  - Directly linked: a GET on ``incident-reports/{event_id}`` retrieves the
    report that was generated for that exact anomaly.
"""

from __future__ import annotations

import logging

from enrichment.opensearch_client import OpenSearchClient
from report.models import IncidentReport

logger = logging.getLogger(__name__)

INDEX = "incident-reports"

# Explicit field types for the fields we query / aggregate on.
# Dynamic mapping handles nested objects (oncall_owner, metrics).
_INDEX_MAPPING: dict = {
    "mappings": {
        "properties": {
            "report_id":             {"type": "keyword"},
            "event_id":              {"type": "keyword"},
            "service":               {"type": "keyword"},
            "severity":              {"type": "keyword"},
            "anomaly_type":          {"type": "keyword"},
            "timestamp":             {"type": "date"},
            "generated_at":          {"type": "date"},
            "summary": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
            },
            "affected_services":     {"type": "keyword"},
            "probable_cause":        {"type": "text"},
            "evidence":              {"type": "text"},
            "suggested_actions":     {"type": "text"},
            "estimated_user_impact": {"type": "text"},
            "confidence_score":      {"type": "float"},
            "ai_generated":          {"type": "boolean"},
            "z_score":               {"type": "float"},
            "pii_scrub_count":       {"type": "integer"},
            "oncall_owner":          {"type": "object",  "dynamic": True},
            "observed_metrics":      {"type": "object",  "dynamic": True},
            "baseline_metrics":      {"type": "object",  "dynamic": True},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}


class ReportStore:
    """Writes and retrieves ``IncidentReport`` documents from OpenSearch."""

    def __init__(self, opensearch: OpenSearchClient) -> None:
        self._os = opensearch

    # ── write ─────────────────────────────────────────────────────────────

    def store(self, report: IncidentReport) -> bool:
        """Index *report* in OpenSearch.

        Uses ``event_id`` as the document ID so the report can be looked up
        directly from any reference to the originating AnomalyEvent.
        Returns ``True`` on success, ``False`` on failure.
        """
        ok = self._os.index_document(
            index=INDEX,
            doc_id=report.event_id,
            document=report.to_dict(),
        )
        if ok:
            logger.info(
                "Stored incident report | report_id=%s event_id=%s "
                "service=%s severity=%s ai_generated=%s",
                report.report_id,
                report.event_id,
                report.service,
                report.severity,
                report.ai_generated,
            )
        else:
            logger.error(
                "Failed to store incident report | report_id=%s event_id=%s",
                report.report_id,
                report.event_id,
            )
        return ok

    # ── index management ──────────────────────────────────────────────────

    def ensure_index(self) -> None:
        """Create the ``incident-reports`` index with mappings if absent.

        Safe to call on every startup — idempotent.
        """
        try:
            if not self._os._client.indices.exists(index=INDEX):
                self._os._client.indices.create(index=INDEX, body=_INDEX_MAPPING)
                logger.info("Created OpenSearch index: %s", INDEX)
            else:
                logger.debug("OpenSearch index already exists: %s", INDEX)
        except Exception as exc:
            logger.warning(
                "ensure_index failed for %s (service will continue): %s",
                INDEX, exc,
            )
