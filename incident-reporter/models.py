"""Top-level data models shared across all Phase 3 stages."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class EnrichedContext:
    """Intermediate result produced by EnrichmentService."""

    log_samples: list = field(default_factory=list)
    correlated_anomalies: list = field(default_factory=list)
    recent_deployments: list = field(default_factory=list)
    oncall_owner: Optional[dict] = None


@dataclass
class EnrichedAnomalyEvent:
    """AnomalyEvent enriched with OpenSearch context and registry data."""

    # ── original AnomalyEvent fields ────────────────────────────────────
    event_id: str
    service: str
    anomaly_type: str
    severity: str
    z_score: float
    baseline: dict
    observed: dict
    top_log_samples: list
    correlated_services: list
    timestamp: str

    # ── enrichment fields ────────────────────────────────────────────────
    log_samples: list
    correlated_anomalies: list
    recent_deployments: list
    oncall_owner: Optional[dict]

    # ── Phase 3 report fields (populated in later parts) ────────────────
    pii_scrub_count: int = 0

    @classmethod
    def from_event_and_context(
        cls, event: dict, ctx: EnrichedContext
    ) -> "EnrichedAnomalyEvent":
        return cls(
            event_id=event["event_id"],
            service=event["service"],
            anomaly_type=event["anomaly_type"],
            severity=event["severity"],
            z_score=float(event.get("z_score", 0.0)),
            baseline=event.get("baseline", {}),
            observed=event.get("observed", {}),
            top_log_samples=event.get("top_log_samples", []),
            correlated_services=event.get("correlated_services", []),
            timestamp=event["timestamp"],
            log_samples=ctx.log_samples,
            correlated_anomalies=ctx.correlated_anomalies,
            recent_deployments=ctx.recent_deployments,
            oncall_owner=ctx.oncall_owner,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)
