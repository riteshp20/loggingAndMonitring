"""OpenSearch client for all Phase 3 queries and writes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_LOGS_INDEX = "logs-*"
_DEPLOYMENTS_INDEX = "deployments"
_INCIDENT_REPORTS_INDEX = "incident-reports"


class OpenSearchClient:
    """Thin wrapper around opensearch-py with retry / graceful-degradation."""

    def __init__(
        self,
        url: str = "http://opensearch:9200",
        username: str = "",
        password: str = "",
    ) -> None:
        from opensearchpy import OpenSearch

        kwargs: dict = {"hosts": [url], "timeout": 5, "max_retries": 2, "retry_on_timeout": True}
        if username and password:
            kwargs["http_auth"] = (username, password)
        self._client = OpenSearch(**kwargs)

    @classmethod
    def with_raw_client(cls, raw_client: object) -> "OpenSearchClient":
        """Inject a pre-built (or mock) client — used in tests."""
        obj = cls.__new__(cls)
        obj._client = raw_client
        return obj

    # ------------------------------------------------------------------ #
    # Log samples                                                          #
    # ------------------------------------------------------------------ #

    def fetch_log_samples(
        self, service: str, timestamp: str, limit: int = 20
    ) -> list[dict]:
        """Return up to *limit* log records for *service* around *timestamp*.

        Searches a 5-minute window ending 1 minute after the anomaly so the
        records that triggered the alert are always included.
        """
        try:
            dt = _parse_iso(timestamp)
            start = (dt - timedelta(minutes=5)).isoformat()
            end = (dt + timedelta(minutes=1)).isoformat()

            body = {
                "size": limit,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"service_name.keyword": service}},
                            {"range": {"@timestamp": {"gte": start, "lte": end}}},
                        ]
                    }
                },
            }
            resp = self._client.search(index=_LOGS_INDEX, body=body)
            return [hit["_source"] for hit in resp["hits"]["hits"]]
        except Exception as exc:
            logger.warning("fetch_log_samples(%s): %s", service, exc)
            return []

    # ------------------------------------------------------------------ #
    # Cross-service correlation                                            #
    # ------------------------------------------------------------------ #

    def fetch_correlated_anomalies(
        self, timestamp: str, excluded_service: str
    ) -> list[dict]:
        """Return IncidentReport records from other services within ±2 minutes.

        Queries the *incident-reports* index (written by Part 4).  Returns an
        empty list when the index does not yet exist or contains no records in
        the window — both are normal during initial roll-out.
        """
        try:
            dt = _parse_iso(timestamp)
            start = (dt - timedelta(minutes=2)).isoformat()
            end = (dt + timedelta(minutes=2)).isoformat()

            body = {
                "size": 20,
                "sort": [{"timestamp": {"order": "desc"}}],
                "_source": ["event_id", "service", "anomaly_type", "severity",
                            "timestamp", "z_score", "summary"],
                "query": {
                    "bool": {
                        "filter": [
                            {"range": {"timestamp": {"gte": start, "lte": end}}},
                        ],
                        "must_not": [
                            {"term": {"service.keyword": excluded_service}},
                        ],
                    }
                },
            }
            resp = self._client.search(index=_INCIDENT_REPORTS_INDEX, body=body)
            return [hit["_source"] for hit in resp["hits"]["hits"]]
        except Exception as exc:
            logger.info("fetch_correlated_anomalies: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # Deployment history                                                   #
    # ------------------------------------------------------------------ #

    def fetch_recent_deployments(
        self, service: str, limit: int = 3
    ) -> list[dict]:
        """Return the last *limit* deployment events for *service*.

        Returns an empty list when the *deployments* index is absent — this is
        expected in environments where deployment tracking is not configured.
        """
        try:
            body = {
                "size": limit,
                "sort": [{"timestamp": {"order": "desc"}}],
                "query": {"term": {"service.keyword": service}},
            }
            resp = self._client.search(index=_DEPLOYMENTS_INDEX, body=body)
            return [hit["_source"] for hit in resp["hits"]["hits"]]
        except Exception as exc:
            logger.info("fetch_recent_deployments(%s): %s", service, exc)
            return []

    # ------------------------------------------------------------------ #
    # Write (used by Part 4)                                               #
    # ------------------------------------------------------------------ #

    def index_document(
        self, index: str, doc_id: str, document: dict
    ) -> bool:
        """Index *document* under *doc_id*.  Returns True on success."""
        try:
            self._client.index(index=index, id=doc_id, body=document)
            return True
        except Exception as exc:
            logger.error("index_document(%s/%s): %s", index, doc_id, exc)
            return False


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
