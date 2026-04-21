"""
Unit tests for Phase 4 Part 4 — RateLimiter (sliding-window, injectable clock).
"""

import threading
import time

import pytest

from alert_router.rate_limiter import (
    MAX_ALERTS_PER_WINDOW,
    WINDOW_SECONDS,
    RateLimiter,
)


# ── helpers ───────────────────────────────────────────────────────────────────


class FakeClock:
    """Monotonically-advanceable fake clock for deterministic tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _limiter(clock: FakeClock | None = None, max_alerts: int = MAX_ALERTS_PER_WINDOW) -> RateLimiter:
    return RateLimiter(max_alerts=max_alerts, _now_fn=clock)


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_max_alerts_is_ten(self):
        assert MAX_ALERTS_PER_WINDOW == 10

    def test_window_is_one_hour(self):
        assert WINDOW_SECONDS == 3600


# ═══════════════════════════════════════════════════════════════════════════════
# Basic allow / deny
# ═══════════════════════════════════════════════════════════════════════════════


class TestAllowDeny:
    def test_first_alert_is_allowed(self):
        clock = FakeClock()
        allowed, _ = _limiter(clock).check_and_record("svc")
        assert allowed is True

    def test_returns_false_digest_queued_when_allowed(self):
        clock = FakeClock()
        _, digest = _limiter(clock).check_and_record("svc")
        assert digest is False

    def test_tenth_alert_is_allowed(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(9):
            lim.check_and_record("svc")
        allowed, _ = lim.check_and_record("svc")
        assert allowed is True

    def test_eleventh_alert_is_denied(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        allowed, _ = lim.check_and_record("svc")
        assert allowed is False

    def test_denied_returns_digest_queued_true(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        _, digest = lim.check_and_record("svc")
        assert digest is True

    def test_many_excess_alerts_all_denied(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        for _ in range(5):
            allowed, _ = lim.check_and_record("svc")
            assert allowed is False

    def test_custom_max_respected(self):
        clock = FakeClock()
        lim = _limiter(clock, max_alerts=3)
        for _ in range(3):
            lim.check_and_record("svc")
        allowed, _ = lim.check_and_record("svc")
        assert allowed is False


# ═══════════════════════════════════════════════════════════════════════════════
# Sliding window behaviour
# ═══════════════════════════════════════════════════════════════════════════════


class TestSlidingWindow:
    def test_alerts_expire_after_window(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        # Advance beyond 1-hour window
        clock.advance(WINDOW_SECONDS + 1)
        allowed, _ = lim.check_and_record("svc")
        assert allowed is True

    def test_window_is_sliding_not_fixed(self):
        clock = FakeClock()
        lim = _limiter(clock)
        # Fill first 5 alerts at t=0
        for _ in range(5):
            lim.check_and_record("svc")
        # Fill next 5 alerts at t=1800 (30 min later)
        clock.advance(1800)
        for _ in range(5):
            lim.check_and_record("svc")
        # At t=3601: first 5 have expired, 5 from t=1800 still in window
        clock.advance(WINDOW_SECONDS - 1800 + 1)
        allowed, _ = lim.check_and_record("svc")
        assert allowed is True  # 6th in new slide, not 11th

    def test_partially_expired_alerts_counted_correctly(self):
        clock = FakeClock()
        lim = _limiter(clock, max_alerts=5)
        for _ in range(3):
            lim.check_and_record("svc")
        clock.advance(WINDOW_SECONDS + 1)  # all 3 expire
        for _ in range(4):
            lim.check_and_record("svc")
        # 4 in window (limit 5) → 5th allowed, 6th denied
        allowed5, _ = lim.check_and_record("svc")
        allowed6, _ = lim.check_and_record("svc")
        assert allowed5 is True
        assert allowed6 is False

    def test_window_refills_after_full_expiry(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        clock.advance(WINDOW_SECONDS + 1)
        results = [lim.check_and_record("svc")[0] for _ in range(MAX_ALERTS_PER_WINDOW)]
        assert all(results)


# ═══════════════════════════════════════════════════════════════════════════════
# current_count()
# ═══════════════════════════════════════════════════════════════════════════════


class TestCurrentCount:
    def test_zero_for_unknown_service(self):
        assert _limiter().current_count("unknown") == 0

    def test_increments_on_each_allowed_alert(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for i in range(1, 6):
            lim.check_and_record("svc")
            assert lim.current_count("svc") == i

    def test_stays_at_max_when_denied(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        lim.check_and_record("svc")  # denied — should not increment
        assert lim.current_count("svc") == MAX_ALERTS_PER_WINDOW

    def test_decrements_as_alerts_expire(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(5):
            lim.check_and_record("svc")
        clock.advance(WINDOW_SECONDS + 1)
        assert lim.current_count("svc") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# reset() / reset_all()
# ═══════════════════════════════════════════════════════════════════════════════


class TestReset:
    def test_reset_clears_window_for_service(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc")
        lim.reset("svc")
        allowed, _ = lim.check_and_record("svc")
        assert allowed is True

    def test_reset_does_not_affect_other_services(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc-a")
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc-b")
        lim.reset("svc-a")
        # svc-b still at limit
        allowed_b, _ = lim.check_and_record("svc-b")
        assert allowed_b is False
        # svc-a is cleared
        allowed_a, _ = lim.check_and_record("svc-a")
        assert allowed_a is True

    def test_reset_unknown_service_does_not_raise(self):
        _limiter().reset("nonexistent")  # must not raise

    def test_reset_all_clears_all_services(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for svc in ("svc-a", "svc-b", "svc-c"):
            for _ in range(MAX_ALERTS_PER_WINDOW):
                lim.check_and_record(svc)
        lim.reset_all()
        for svc in ("svc-a", "svc-b", "svc-c"):
            assert lim.current_count(svc) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Service isolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestServiceIsolation:
    def test_different_services_have_independent_windows(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("svc-a")
        allowed_b, _ = lim.check_and_record("svc-b")
        assert allowed_b is True

    def test_filling_one_service_does_not_affect_another(self):
        clock = FakeClock()
        lim = _limiter(clock)
        for _ in range(MAX_ALERTS_PER_WINDOW):
            lim.check_and_record("payment-svc")
        for _ in range(MAX_ALERTS_PER_WINDOW):
            allowed, _ = lim.check_and_record("auth-svc")
            assert allowed is True


# ═══════════════════════════════════════════════════════════════════════════════
# Thread safety (smoke test)
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    def test_concurrent_calls_do_not_raise(self):
        lim = RateLimiter()
        errors: list[Exception] = []

        def work():
            try:
                for _ in range(20):
                    lim.check_and_record("shared-svc")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"

    def test_concurrent_calls_never_exceed_max(self):
        lim = RateLimiter()
        allowed_count = 0
        lock = threading.Lock()

        def work():
            nonlocal allowed_count
            for _ in range(5):
                ok, _ = lim.check_and_record("svc")
                if ok:
                    with lock:
                        allowed_count += 1

        threads = [threading.Thread(target=work) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert allowed_count <= MAX_ALERTS_PER_WINDOW
