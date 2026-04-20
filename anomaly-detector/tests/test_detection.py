"""
Unit tests for the two-tier detection pipeline.
All Redis I/O uses fakeredis — no external process required.
"""

import math
import random
import time

import fakeredis
import pytest

from baseline.manager import BaselineManager
from detection.detector import TwoTierDetector, ZSCORE_CRITICAL_THRESHOLD
from detection.tier1_zscore import ZScoreDetector, ZSCORE_THRESHOLD
from detection.tier2_isolation import IsolationForestDetector, TIER2_THRESHOLD, FEATURE_KEYS

# ── shared fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def mgr(r):
    return BaselineManager(r, ewma_alpha=0.1)


@pytest.fixture
def t1(mgr):
    return ZScoreDetector(mgr)


@pytest.fixture
def t2(r):
    # Low minimum so tests can train a model with a handful of samples.
    return IsolationForestDetector(r, min_training_samples=20)


@pytest.fixture
def detector(mgr, t1, t2):
    return TwoTierDetector(mgr, t1, t2)


# ── representative feature vectors ────────────────────────────────────────────

NORMAL = {
    "error_rate": 0.01,
    "p99_latency_ms": 200.0,
    "avg_latency_ms": 80.0,
    "request_volume": 1000.0,
    "warn_rate": 0.02,
}

# Extreme vector — all dimensions are catastrophically far from normal.
ANOMALOUS = {
    "error_rate": 0.95,
    "p99_latency_ms": 45_000.0,
    "avg_latency_ms": 30_000.0,
    "request_volume": 1.0,
    "warn_rate": 0.90,
}

# Moderate anomaly — designed so that, with the tight baselines in the "both_tiers"
# test, max |z| ≈ 4.0 (fires Tier 1 but stays below the critical threshold of 5.0).
# It is also 10-30 σ from the IF training distribution, so IF reliably flags it.
MODERATE_ANOMALY = {
    "error_rate": 0.03,       # ewma=0.01, std=0.005 → z=4.0
    "p99_latency_ms": 250.0,  # ewma=200,  std=12.5  → z=4.0
    "avg_latency_ms": 110.0,  # ewma=80,   std=7.5   → z=4.0
    "request_volume": 800.0,  # ewma=1000, std=50    → |z|=4.0
    "warn_rate": 0.05,        # ewma=0.02, std=0.0075→ z=4.0
}


def _normal_with_noise(rng: random.Random) -> dict:
    """Gaussian-noised normal sample for IF training (prevents identical-vector bias)."""
    return {
        "error_rate":     max(0.0, 0.01  + rng.gauss(0, 0.001)),
        "p99_latency_ms": max(1.0, 200.0 + rng.gauss(0, 5.0)),
        "avg_latency_ms": max(1.0, 80.0  + rng.gauss(0, 3.0)),
        "request_volume": max(1.0, 1000.0+ rng.gauss(0, 30.0)),
        "warn_rate":      max(0.0, 0.02  + rng.gauss(0, 0.001)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 — Z-score detector
# ═══════════════════════════════════════════════════════════════════════════════


class TestZScoreDetector:
    def test_no_baseline_returns_zero_z_not_fired(self, t1):
        result = t1.check("svc", {"error_rate": 0.99})
        assert result.fired is False
        assert result.max_z_score == 0.0

    def test_zero_std_dev_returns_zero_z(self, t1):
        # Feed constant values to drive std_dev → 0 in the baseline
        for _ in range(20):
            t1.check("svc", {"latency": 100.0})
        result = t1.check("svc", {"latency": 100.0})
        assert result.z_scores["latency"] == 0.0

    def test_normal_value_does_not_fire(self, mgr, t1, r):
        # Pre-seed baseline: ewma=50, std_dev=5  →  z for 52 = (52-50)/5 = 0.4
        r.hset("svc:svc-z:baseline:latency", mapping={
            "mean": 50.0, "std_dev": 5.0, "ewma": 50.0,
            "sample_count": 100, "last_updated": time.time(),
        })
        result = t1.check("svc-z", {"latency": 52.0})
        assert result.fired is False
        assert abs(result.z_scores["latency"]) < ZSCORE_THRESHOLD

    def test_extreme_value_fires(self, mgr, t1, r):
        # z for 80 = (80-50)/5 = 6.0 > 3.0  →  must fire
        r.hset("svc:svc-z2:baseline:latency", mapping={
            "mean": 50.0, "std_dev": 5.0, "ewma": 50.0,
            "sample_count": 100, "last_updated": time.time(),
        })
        result = t1.check("svc-z2", {"latency": 80.0})
        assert result.fired is True
        assert result.max_z_score == pytest.approx(6.0, abs=1e-9)

    def test_max_z_score_is_worst_metric(self, t1, r):
        # latency z = 6.0, error_rate z = 1.0 → max should be 6.0
        r.hset("svc:svc-m:baseline:latency", mapping={
            "mean": 50.0, "std_dev": 5.0, "ewma": 50.0,
            "sample_count": 100, "last_updated": time.time(),
        })
        r.hset("svc:svc-m:baseline:error_rate", mapping={
            "mean": 0.01, "std_dev": 0.01, "ewma": 0.01,
            "sample_count": 100, "last_updated": time.time(),
        })
        result = t1.check("svc-m", {"latency": 80.0, "error_rate": 0.02})
        assert result.max_z_score == pytest.approx(6.0, abs=1e-9)

    def test_non_numeric_fields_are_skipped(self, t1):
        result = t1.check("svc", {"service": "my-svc", "latency": 50.0})
        assert "service" not in result.z_scores
        assert "latency" in result.z_scores

    def test_baseline_updated_after_check(self, mgr, t1):
        t1.check("svc-upd", {"latency": 100.0})
        baseline = mgr.get_baseline("svc-upd", "latency")
        assert baseline is not None
        assert baseline.sample_count == 1

    def test_check_uses_pre_update_baseline_for_z(self, t1, r):
        """
        z-score must be computed against the OLD ewma, not the one that already
        includes the current observation.
        """
        r.hset("svc:svc-pre:baseline:latency", mapping={
            "mean": 50.0, "std_dev": 5.0, "ewma": 50.0,
            "sample_count": 100, "last_updated": time.time(),
        })
        result = t1.check("svc-pre", {"latency": 65.0})
        # Expected z = (65 - 50) / 5 = 3.0 (boundary)
        assert result.z_scores["latency"] == pytest.approx(3.0, abs=1e-9)


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2 — Isolation Forest detector
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsolationForestDetector:
    def test_no_model_not_fired(self, t2):
        fired, score, available = t2.score("fresh-svc", NORMAL)
        assert fired is False
        assert available is False
        assert score == 0.0

    def test_insufficient_samples_no_model(self, r):
        det = IsolationForestDetector(r, min_training_samples=50)
        for _ in range(10):  # Only 10, need 50
            det.store_training_sample("svc", NORMAL)
        retrained = det.maybe_retrain("svc")
        assert retrained is False
        assert det._load_model("svc") is None

    def test_model_trained_with_enough_samples(self, t2):
        for _ in range(20):
            t2.store_training_sample("svc-train", NORMAL)
        retrained = t2.maybe_retrain("svc-train")
        assert retrained is True
        assert t2._load_model("svc-train") is not None

    def test_normal_vector_not_flagged(self, t2):
        # Train on varied normal data so IF has a real distribution to learn.
        rng = random.Random(42)
        for _ in range(200):
            t2.store_training_sample("svc-norm", _normal_with_noise(rng))
        t2.maybe_retrain("svc-norm")
        # Score the canonical mean normal vector — must not be flagged.
        fired, score, available = t2.score("svc-norm", NORMAL)
        assert available is True
        assert fired is False

    def test_extreme_anomaly_scores_below_threshold(self, t2):
        # Identical training vectors give IsolationForest nothing to learn from.
        # Use Gaussian-noised samples so the model has a real distribution.
        rng = random.Random(7)
        for _ in range(200):
            t2.store_training_sample("svc-anom", _normal_with_noise(rng))
        t2.maybe_retrain("svc-anom")
        _, score, available = t2.score("svc-anom", ANOMALOUS)
        assert available is True
        # ANOMALOUS is catastrophically far from the training distribution.
        assert score < 0.0

    def test_retraining_skipped_when_model_fresh(self, t2, r):
        for _ in range(20):
            t2.store_training_sample("svc-fresh", NORMAL)
        t2.maybe_retrain("svc-fresh")               # Initial train
        r.set("if_model:svc-fresh:last_trained", time.time())  # Reset to "just now"
        retrained = t2.maybe_retrain("svc-fresh")
        assert retrained is False

    def test_old_training_samples_pruned(self, t2, r):
        old_ts = time.time() - 8 * 86_400   # 8 days ago
        key = "if_model:svc-prune:training"
        r.zadd(key, {f"[0.01, 200.0, 80.0, 1000.0, 0.02]:{old_ts:.6f}": old_ts})
        t2.store_training_sample("svc-prune", NORMAL)  # triggers prune
        remaining = r.zcard(key)
        assert remaining == 1  # only the fresh entry

    def test_model_available_flag_false_without_model(self, t2):
        _, _, available = t2.score("never-trained", NORMAL)
        assert available is False

    def test_score_returns_float(self, t2):
        for _ in range(20):
            t2.store_training_sample("svc-float", NORMAL)
        t2.maybe_retrain("svc-float")
        _, score, _ = t2.score("svc-float", NORMAL)
        assert isinstance(score, float)


# ═══════════════════════════════════════════════════════════════════════════════
# TwoTierDetector — verdict logic
# ═══════════════════════════════════════════════════════════════════════════════


def _seed_baseline(r, service: str, metric: str, ewma: float, std_dev: float) -> None:
    """Inject a synthetic baseline so the next check produces a predictable z-score."""
    r.hset(f"svc:{service}:baseline:{metric}", mapping={
        "mean": ewma, "std_dev": std_dev, "ewma": ewma,
        "sample_count": 100, "last_updated": time.time(),
    })
    # Mark service as past the cold-start window
    r.set(f"svc:{service}:first_seen", time.time() - 25 * 3_600)


class TestTwoTierVerdicts:
    def test_no_anomaly_neither_tier_fires(self, detector, r):
        _seed_baseline(r, "svc-nn", "error_rate", ewma=0.01, std_dev=0.01)
        result = detector.detect("svc-nn", {"error_rate": 0.012})
        assert result.is_anomaly is False
        assert result.verdict_reason == "none"

    def test_only_tier1_fires_no_anomaly(self, detector, r):
        # z = (0.05 - 0.01) / 0.01 = 4.0 → Tier 1 fires, but Tier 2 won't fire
        # (no model yet for this service)
        _seed_baseline(r, "svc-t1", "error_rate", ewma=0.01, std_dev=0.01)
        result = detector.detect("svc-t1", {"error_rate": 0.05})
        assert result.tier1.fired is True
        assert result.tier2.model_available is False
        assert result.is_anomaly is False
        assert result.verdict_reason == "none"

    def test_both_tiers_fire_is_anomaly(self, detector, r, t2):
        """
        'both_tiers' verdict requires Tier 1 to fire (|z| > 3) AND Tier 2 to fire,
        with max |z| staying below the critical threshold of 5.0.

        MODERATE_ANOMALY is crafted so that each metric gives z ≈ 4.0 against
        the seeded baselines below.  It is also 10-30 σ from the IF training
        distribution, so Tier 2 reliably fires.
        """
        # Train Tier 2 on tightly clustered normal data with Gaussian noise.
        rng = random.Random(0)
        for _ in range(300):
            t2.store_training_sample("svc-both", _normal_with_noise(rng))
        t2.maybe_retrain("svc-both")

        # Seed baselines so each MODERATE_ANOMALY metric gives z ≈ 4.0 (< 5.0).
        _seed_baseline(r, "svc-both", "error_rate",     ewma=0.01,   std_dev=0.005)
        _seed_baseline(r, "svc-both", "p99_latency_ms", ewma=200.0,  std_dev=12.5)
        _seed_baseline(r, "svc-both", "avg_latency_ms", ewma=80.0,   std_dev=7.5)
        _seed_baseline(r, "svc-both", "request_volume", ewma=1000.0, std_dev=50.0)
        _seed_baseline(r, "svc-both", "warn_rate",      ewma=0.02,   std_dev=0.0075)

        result = detector.detect("svc-both", MODERATE_ANOMALY)

        assert result.tier1.fired is True
        assert result.tier1.max_z_score < ZSCORE_CRITICAL_THRESHOLD  # must not be critical
        assert result.tier2.model_available is True
        assert result.tier2.fired is True   # 10-30σ outlier in IF space
        assert result.is_anomaly is True
        assert result.verdict_reason == "both_tiers"

    def test_zscore_critical_bypasses_tier2(self, detector, r):
        # z = (600 - 50) / 5 = 110 >> 5.0  →  critical bypass
        _seed_baseline(r, "svc-crit", "latency", ewma=50.0, std_dev=5.0)
        result = detector.detect("svc-crit", {"latency": 600.0})
        assert result.tier1.max_z_score > ZSCORE_CRITICAL_THRESHOLD
        assert result.is_anomaly is True
        assert result.verdict_reason == "zscore_critical"

    def test_learning_suppresses_anomaly(self, detector, r):
        # Do NOT call _seed_baseline — service will be brand new → learning=True
        result = detector.detect("svc-learn", {"error_rate": 0.99})
        assert result.learning is True
        assert result.is_anomaly is False
        assert result.verdict_reason == "learning"

    def test_learning_flag_false_after_cold_start(self, detector, r):
        r.set("svc:svc-cold:first_seen", time.time() - 25 * 3_600)
        result = detector.detect("svc-cold", {"error_rate": 0.01})
        assert result.learning is False

    def test_result_fields_always_populated(self, detector, r):
        _seed_baseline(r, "svc-fields", "error_rate", ewma=0.01, std_dev=0.01)
        result = detector.detect("svc-fields", {"error_rate": 0.01})
        assert result.service == "svc-fields"
        assert result.timestamp > 0
        assert isinstance(result.tier1.z_scores, dict)
        assert isinstance(result.tier2.anomaly_score, float)
        assert result.verdict_reason in {"both_tiers", "zscore_critical", "learning", "none"}
