"""
Tier 1 — Fast Z-score anomaly check.

For each numeric metric in the incoming feature vector:
  1. Read the existing EWMA baseline for (service, metric).
  2. Update the baseline with the new observation.
  3. Compute z = (value − ewma_old) / std_dev_old.

Using the PRE-UPDATE baseline as the reference point means the current
observation is evaluated against historical behaviour, not contaminated by itself.

Fires when max(|z|) across all metrics exceeds ZSCORE_THRESHOLD (3.0).
Expected round-trip latency: < 5 ms on a local Redis (mostly network + pipeline).
"""

from baseline.manager import BaselineManager
from .models import ZScoreResult

ZSCORE_THRESHOLD = 3.0


class ZScoreDetector:
    def __init__(self, baseline_manager: BaselineManager) -> None:
        self._baseline = baseline_manager

    def check(self, service: str, features: dict) -> ZScoreResult:
        """
        Evaluate every numeric field in *features* against its stored baseline.
        Baselines are updated in-place so subsequent calls see the latest state.

        Returns ZScoreResult with per-metric z-scores and a fired flag.
        """
        z_scores: dict[str, float] = {}

        for metric, value in features.items():
            if not isinstance(value, (int, float)):
                continue

            value = float(value)

            # Capture old baseline BEFORE updating (z-score uses historical state).
            old_baseline = self._baseline.get_baseline(service, metric)

            # Ingest the new observation (updates EWMA + rolling window).
            self._baseline.update_baseline(service, metric, value)

            if old_baseline is None or old_baseline.std_dev == 0.0:
                # No history yet, or zero variance — cannot produce a meaningful z-score.
                z_scores[metric] = 0.0
                continue

            z = (value - old_baseline.ewma) / old_baseline.std_dev
            z_scores[metric] = z

        max_z = max((abs(z) for z in z_scores.values()), default=0.0)
        fired = max_z > ZSCORE_THRESHOLD

        return ZScoreResult(fired=fired, max_z_score=max_z, z_scores=z_scores)
