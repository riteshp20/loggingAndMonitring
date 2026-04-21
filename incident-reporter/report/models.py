"""IncidentReport — the final Phase 3 output linked to an AnomalyEvent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class IncidentReport:
    # ── identity ──────────────────────────────────────────────────────────
    report_id: str          # UUID4 — unique per report
    event_id: str           # links back to the originating AnomalyEvent
    service: str
    severity: str
    anomaly_type: str
    timestamp: str          # ISO-8601 UTC of the original anomaly
    generated_at: str       # ISO-8601 UTC when this report was generated

    # ── AI-generated (or fallback) analysis ──────────────────────────────
    summary: str
    affected_services: list
    probable_cause: str
    evidence: list
    suggested_actions: list
    estimated_user_impact: str
    confidence_score: float  # 0.0–1.0

    # ── metadata ──────────────────────────────────────────────────────────
    ai_generated: bool       # False when Claude API is unavailable (fallback path)
    oncall_owner: Optional[dict]
    z_score: float
    observed_metrics: dict
    baseline_metrics: dict
    pii_scrub_count: int = 0  # populated by Part 3 (PII scrubber)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)
