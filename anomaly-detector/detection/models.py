from dataclasses import dataclass


@dataclass
class ZScoreResult:
    fired: bool
    max_z_score: float
    z_scores: dict  # {metric: z_score}


@dataclass
class IsolationForestResult:
    fired: bool
    anomaly_score: float   # sklearn score_samples() output (higher = more normal)
    model_available: bool  # False during cold-start before enough training data


@dataclass
class DetectionResult:
    service: str
    timestamp: float

    tier1: ZScoreResult
    tier2: IsolationForestResult

    is_anomaly: bool
    # "both_tiers" | "zscore_critical" | "learning" | "none"
    verdict_reason: str
    learning: bool  # True when service is within cold-start window
