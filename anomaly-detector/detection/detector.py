"""
TwoTierDetector — orchestrates Tier 1 (Z-score) and Tier 2 (Isolation Forest).

Verdict logic
─────────────
  is_anomaly = True   if service is NOT in learning window AND
                         (BOTH tiers fire  OR  Tier 1 max |z| > 5.0)

  verdict_reason values
    "zscore_critical"  – Tier 1 max |z| > 5.0; bypasses Tier 2 requirement
    "both_tiers"       – Tier 1 AND Tier 2 both fired
    "learning"         – service is within 24-hour cold-start window; suppressed
    "none"             – no anomaly detected
"""

import time

from baseline.manager import BaselineManager
from .models import DetectionResult, IsolationForestResult
from .tier1_zscore import ZScoreDetector
from .tier2_isolation import IsolationForestDetector

ZSCORE_CRITICAL_THRESHOLD: float = 5.0


class TwoTierDetector:
    def __init__(
        self,
        baseline_manager: BaselineManager,
        tier1: ZScoreDetector,
        tier2: IsolationForestDetector,
    ) -> None:
        self._baseline = baseline_manager
        self._t1 = tier1
        self._t2 = tier2

    def detect(self, service: str, features: dict) -> DetectionResult:
        """
        Run the full two-tier pipeline on one feature vector for *service*.

        Steps
        1. Tier 1 – Z-score: evaluates each metric against its EWMA baseline
           and updates the baseline with the new observation.
        2. Tier 2 – Isolation Forest: stores the sample in the training ZSET,
           triggers lazy retraining if the model is stale, then scores the vector.
        3. Apply verdict rules and return a fully populated DetectionResult.
        """
        now = time.time()
        learning = self._baseline.is_learning(service)

        # — Tier 1 —
        t1 = self._t1.check(service, features)

        # — Tier 2 —
        self._t2.store_training_sample(service, features)
        tier2_fired, anomaly_score, model_available = self._t2.score(service, features)
        t2 = IsolationForestResult(
            fired=tier2_fired,
            anomaly_score=anomaly_score,
            model_available=model_available,
        )

        # — Verdict —
        if learning:
            is_anomaly = False
            reason = "learning"
        elif t1.max_z_score > ZSCORE_CRITICAL_THRESHOLD:
            is_anomaly = True
            reason = "zscore_critical"
        elif t1.fired and t2.fired:
            is_anomaly = True
            reason = "both_tiers"
        else:
            is_anomaly = False
            reason = "none"

        return DetectionResult(
            service=service,
            timestamp=now,
            tier1=t1,
            tier2=t2,
            is_anomaly=is_anomaly,
            verdict_reason=reason,
            learning=learning,
        )
