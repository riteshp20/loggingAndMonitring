"""
Unit tests for BaselineManager.
All Redis I/O is handled by fakeredis — no external process required.
"""

import math
import time

import fakeredis
import pytest

from baseline.manager import BaselineManager
from baseline.models import BaselineData


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def mgr(r):
    return BaselineManager(r, ewma_alpha=0.1, window_days=7, cold_start_hours=24)


# ── cold-start ────────────────────────────────────────────────────────────────


class TestColdStart:
    def test_brand_new_service_is_learning(self, mgr):
        assert mgr.is_learning("new-svc") is True

    def test_service_registered_now_is_learning(self, mgr):
        mgr._register_service("svc-a")
        assert mgr.is_learning("svc-a") is True

    def test_service_registered_25h_ago_is_not_learning(self, mgr, r):
        r.set("svc:svc-b:first_seen", time.time() - 25 * 3_600)
        assert mgr.is_learning("svc-b") is False

    def test_update_registers_first_seen(self, mgr):
        mgr.update_baseline("svc-c", "error_rate", 0.01)
        assert mgr._r.get("svc:svc-c:first_seen") is not None

    def test_first_seen_not_overwritten_on_second_update(self, mgr, r):
        t0 = time.time() - 1_000
        r.set("svc:svc-d:first_seen", t0)
        mgr.update_baseline("svc-d", "error_rate", 0.01)
        stored = float(r.get("svc:svc-d:first_seen"))
        assert stored == pytest.approx(t0, abs=1e-3)


# ── baseline initialisation ───────────────────────────────────────────────────


class TestBaselineInit:
    def test_get_baseline_unknown_returns_none(self, mgr):
        assert mgr.get_baseline("ghost", "latency") is None

    def test_first_update_initialises_all_fields(self, mgr):
        b = mgr.update_baseline("svc-e", "latency", 200.0)
        assert b.ewma == 200.0
        assert b.mean == 200.0
        assert b.std_dev == 0.0
        assert b.sample_count == 1
        assert b.last_updated > 0

    def test_baseline_round_trips_through_redis(self, mgr):
        mgr.update_baseline("svc-f", "error_rate", 0.05)
        b = mgr.get_baseline("svc-f", "error_rate")
        assert isinstance(b, BaselineData)
        assert b.ewma == pytest.approx(0.05, abs=1e-9)


# ── EWMA update correctness ───────────────────────────────────────────────────


class TestEWMAUpdates:
    def test_second_sample_ewma(self, mgr):
        mgr.update_baseline("svc-g", "latency", 100.0)   # ewma = 100
        b = mgr.update_baseline("svc-g", "latency", 10.0)
        # α=0.1: 0.1*10 + 0.9*100 = 91.0
        assert b.ewma == pytest.approx(91.0, abs=1e-9)

    def test_ewma_alpha_weighting(self, mgr):
        mgr.update_baseline("svc-h", "error_rate", 0.10)  # ewma = 0.10
        b = mgr.update_baseline("svc-h", "error_rate", 0.20)
        # α=0.1: 0.1*0.20 + 0.9*0.10 = 0.11
        assert b.ewma == pytest.approx(0.11, abs=1e-9)

    def test_constant_series_has_zero_std_dev(self, mgr):
        for _ in range(10):
            mgr.update_baseline("svc-i", "latency", 50.0)
        b = mgr.get_baseline("svc-i", "latency")
        assert b.std_dev == pytest.approx(0.0, abs=1e-9)

    def test_high_variance_series_has_positive_std_dev(self, mgr):
        mgr.update_baseline("svc-j", "latency", 0.0)
        for _ in range(30):
            mgr.update_baseline("svc-j", "latency", 0.0)
            mgr.update_baseline("svc-j", "latency", 100.0)
        b = mgr.get_baseline("svc-j", "latency")
        assert b.std_dev > 0

    def test_sample_count_increments(self, mgr):
        for i in range(5):
            b = mgr.update_baseline("svc-k", "error_rate", float(i))
        assert b.sample_count == 5

    def test_running_mean_is_arithmetic_average(self, mgr):
        for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
            b = mgr.update_baseline("svc-l", "latency", v)
        # Arithmetic mean of [10,20,30,40,50] = 30
        assert b.mean == pytest.approx(30.0, abs=1e-9)

    def test_variance_formula_hand_check(self, mgr):
        """
        Two-step hand-verification of EWMA variance.
        Step 1: x=1  → ewma=1, V=0
        Step 2: x=0  → ewma = 0.1*0 + 0.9*1 = 0.9
                        V    = 0.9*0 + 0.1*0.9*(0−1)² = 0.09
                        std  ≈ 0.3
        """
        mgr.update_baseline("svc-m", "e", 1.0)
        b = mgr.update_baseline("svc-m", "e", 0.0)
        # V = 0.1 * 0.9 * (0 - 1)^2 = 0.09
        assert b.std_dev == pytest.approx(math.sqrt(0.09), abs=1e-9)


# ── rolling window ────────────────────────────────────────────────────────────


class TestRollingWindow:
    def test_window_count_reflects_inserts(self, mgr):
        mgr.update_baseline("svc-n", "error_rate", 0.01)
        mgr.update_baseline("svc-n", "error_rate", 0.02)
        assert mgr.window_sample_count("svc-n", "error_rate") == 2

    def test_old_samples_are_pruned(self, mgr, r):
        old_ts = time.time() - 8 * 86_400   # 8 days ago — outside 7-day window
        now_ts = time.time()
        key = "svc:svc-o:samples:latency"
        r.zadd(key, {"old:100.0:aaaabbbb": old_ts})
        r.zadd(key, {"new:100.0:ccccdddd": now_ts})

        # Trigger an update; pruning runs inside the pipeline
        mgr.update_baseline("svc-o", "latency", 100.0)

        # "old" entry should have been evicted; "new" + our new entry remain
        count = mgr.window_sample_count("svc-o", "latency")
        assert count == 2   # pre-seeded "new" + the one we just inserted


# ── service status ────────────────────────────────────────────────────────────


class TestServiceStatus:
    def test_status_learning_true_for_new_service(self, mgr):
        mgr.update_baseline("svc-p", "error_rate", 0.01)
        s = mgr.get_service_status("svc-p")
        assert s["learning"] is True
        assert "error_rate" in s["metrics"]

    def test_status_learning_false_after_cold_start(self, mgr, r):
        r.set("svc:svc-q:first_seen", time.time() - 25 * 3_600)
        mgr.update_baseline("svc-q", "latency", 200.0)
        s = mgr.get_service_status("svc-q")
        assert s["learning"] is False

    def test_status_includes_multiple_metrics(self, mgr):
        mgr.update_baseline("svc-r", "error_rate", 0.03)
        mgr.update_baseline("svc-r", "latency", 180.0)
        s = mgr.get_service_status("svc-r")
        assert "error_rate" in s["metrics"]
        assert "latency" in s["metrics"]

    def test_status_age_hours_is_positive(self, mgr):
        mgr.update_baseline("svc-s", "latency", 50.0)
        s = mgr.get_service_status("svc-s")
        assert s["age_hours"] is not None
        assert s["age_hours"] >= 0
