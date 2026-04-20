"""
AnomalyDetectionPipeline — single entry point that orchestrates all four phases.

  Phase 1  BaselineManager      → maintains per-service EWMA baselines in Redis
  Phase 2  TwoTierDetector      → Z-score (Tier 1) + Isolation Forest (Tier 2)
  Phase 3  AlertSuppressor      → cooldown + storm deduplication
  Phase 4  AnomalyEventEmitter  → serialises and publishes to anomaly-events topic

call process_window() once per ServiceWindowAggregate received from processed-logs.
"""

from dataclasses import asdict
from typing import Optional

from baseline.manager import BaselineManager
from detection.detector import TwoTierDetector
from suppression.suppressor import AlertSuppressor
from events.emitter import AnomalyEventEmitter
from events.models import AnomalyEvent


class AnomalyDetectionPipeline:
    def __init__(
        self,
        baseline_manager: BaselineManager,
        detector: TwoTierDetector,
        suppressor: AlertSuppressor,
        emitter: AnomalyEventEmitter,
    ) -> None:
        self.baseline_mgr = baseline_manager
        self.detector = detector
        self.suppressor = suppressor
        self.emitter = emitter

    def process_window(
        self,
        service: str,
        features: dict,
        top_log_samples: list | None = None,
    ) -> Optional[AnomalyEvent]:
        """
        Run a single ServiceWindowAggregate through the full pipeline.

        Returns the AnomalyEvent that was emitted, or None when:
          - No anomaly was detected
          - The anomaly was suppressed by cooldown
          - A storm alert is already active for the window

        Steps
        1. Detect  — TwoTierDetector updates baselines and runs both tiers.
        2. Suppress — AlertSuppressor applies cooldown + storm coalescing.
        3. Emit    — Build the appropriate AnomalyEvent and publish to Kafka.
        """
        # — 1. Detection —————————————————————————————————————————————————————
        detection = self.detector.detect(service, features)
        if not detection.is_anomaly:
            return None

        # — 2. Suppression ————————————————————————————————————————————————————
        suppress = self.suppressor.check_and_suppress(service)

        if not suppress.allowed and not suppress.is_storm:
            return None  # cooldown or storm_active

        # — 3. Fetch baselines for all numeric metrics ————————————————————————
        baselines: dict = {}
        for metric, value in features.items():
            if isinstance(value, (int, float)):
                b = self.baseline_mgr.get_baseline(service, metric)
                if b:
                    baselines[metric] = asdict(b)

        # — 4. Build and emit event ———————————————————————————————————————————
        if suppress.is_storm:
            event = self.emitter.build_storm_event(
                service, detection, suppress, top_log_samples
            )
        else:
            event = self.emitter.build_event(
                detection,
                features,
                baselines,
                top_log_samples,
                correlated_services=suppress.storm_services,
            )

        self.emitter.emit(event)
        return event
