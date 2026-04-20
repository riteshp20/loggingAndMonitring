import dataclasses
import json
from dataclasses import dataclass


@dataclass
class AnomalyEvent:
    event_id: str             # UUID4 — unique identifier for deduplication / idempotent replay
    service: str              # Originating service name; "system" for system_wide_incident
    anomaly_type: str         # "zscore_critical" | "both_tiers" | "system_wide_incident"
    severity: str             # "P1" | "P2" | "P3"
    z_score: float            # Max |z| from Tier 1 (0.0 for system_wide_incident)
    baseline: dict            # {metric: {mean, std_dev, ewma, sample_count, last_updated}}
    observed: dict            # {metric: observed_value} — the feature vector that fired
    top_log_samples: list     # Up to 20 raw log records from the anomalous window
    correlated_services: list # Services in the storm window (populated for all event types)
    timestamp: str            # ISO-8601 UTC

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
