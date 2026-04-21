"""
Unit tests for AlertSuppressor.
All Redis I/O uses fakeredis — no external process required.

Storm threshold is kept at the production default (>5 distinct services).
Cooldown and storm-window durations are overridden per test where timing matters.
"""

import time

import fakeredis
import pytest

from suppression.suppressor import AlertSuppressor
from suppression.models import SuppressResult


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def sup(r):
    """Suppressor with production defaults (threshold=5, cooldown=600s)."""
    return AlertSuppressor(r)


@pytest.fixture
def fast_sup(r):
    """Suppressor with tiny timeouts so tests can simulate expiry via key deletion."""
    return AlertSuppressor(
        r,
        cooldown_seconds=600,
        storm_window_seconds=60,
        storm_threshold=5,
        storm_cooldown_seconds=300,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Cooldown
# ═══════════════════════════════════════════════════════════════════════════════


class TestCooldown:
    def test_first_alert_is_allowed(self, sup):
        result = sup.check_and_suppress("svc-a")
        assert result.allowed is True
        assert result.reason == "allowed"

    def test_repeat_alert_is_suppressed(self, sup):
        sup.check_and_suppress("svc-a")
        result = sup.check_and_suppress("svc-a")
        assert result.allowed is False
        assert result.reason == "cooldown"

    def test_different_services_both_allowed(self, sup):
        r1 = sup.check_and_suppress("svc-a")
        r2 = sup.check_and_suppress("svc-b")
        assert r1.allowed is True
        assert r2.allowed is True

    def test_cooldown_key_has_correct_ttl(self, sup, r):
        sup.check_and_suppress("svc-a")
        ttl = r.ttl("suppress:cooldown:svc-a")
        assert 0 < ttl <= 600

    def test_simulated_cooldown_expiry_allows_new_alert(self, sup, r):
        sup.check_and_suppress("svc-a")
        r.delete("suppress:cooldown:svc-a")   # simulate TTL expiry
        result = sup.check_and_suppress("svc-a")
        assert result.allowed is True

    def test_cooldown_ttl_helper_returns_positive(self, sup):
        sup.check_and_suppress("svc-a")
        assert sup.cooldown_ttl("svc-a") > 0

    def test_cooldown_ttl_helper_returns_minus2_when_no_cooldown(self, sup):
        assert sup.cooldown_ttl("unknown-svc") == -2

    def test_is_not_storm_during_cooldown_suppression(self, sup):
        sup.check_and_suppress("svc-a")
        result = sup.check_and_suppress("svc-a")
        assert result.is_storm is False


# ═══════════════════════════════════════════════════════════════════════════════
# Storm detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestStormDetection:
    def test_exactly_5_services_is_not_storm(self, sup):
        """Threshold is >5; exactly 5 services must NOT trigger a storm."""
        for i in range(5):
            result = sup.check_and_suppress(f"svc-{i}")
            assert result.allowed is True
            assert result.is_storm is False

    def test_6th_service_triggers_storm(self, sup):
        """6 distinct services within the window crosses the >5 threshold."""
        for i in range(5):
            sup.check_and_suppress(f"svc-{i}")
        result = sup.check_and_suppress("svc-5")
        assert result.allowed is False
        assert result.is_storm is True
        assert result.reason == "storm"

    def test_storm_result_has_correct_service_count(self, sup):
        for i in range(5):
            sup.check_and_suppress(f"svc-{i}")
        result = sup.check_and_suppress("svc-5")
        assert result.storm_service_count == 6

    def test_storm_result_lists_all_services(self, sup):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        # Find the storm result (the one with is_storm=True)
        # Re-check the state via a fresh allowed service to read storm_services
        result = sup.check_and_suppress("svc-5")   # already alerted → suppressed by cooldown
        # svc-5 is in cooldown, but we can check storm_services on any result
        # Test by directly checking storm window state
        count, services = sup._storm_window_state()
        assert count == 6
        for i in range(6):
            assert f"svc-{i}" in services

    def test_storm_active_key_set_after_storm_triggered(self, sup, r):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        assert r.exists("suppress:storm:active")

    def test_storm_active_key_has_ttl(self, sup, r):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        ttl = r.ttl("suppress:storm:active")
        assert 0 < ttl <= 300

    def test_post_storm_alert_is_suppressed_with_storm_active_reason(self, sup):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        # A fresh service (no cooldown) alerts after the storm
        result = sup.check_and_suppress("new-svc-after-storm")
        assert result.allowed is False
        assert result.reason == "storm_active"
        assert result.is_storm is False

    def test_storm_active_flag_false_for_storm_active_reason(self, sup):
        """Once the storm slot is claimed, further services get storm_active, not is_storm."""
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        result = sup.check_and_suppress("another-new-svc")
        assert result.is_storm is False

    def test_old_storm_entries_outside_window_pruned(self, sup, r):
        old_ts = time.time() - 70   # 70s ago — outside the 60-second window
        r.zadd("suppress:storm:alerts", {"stale-svc": old_ts})
        result = sup.check_and_suppress("fresh-svc")
        assert "stale-svc" not in result.storm_services

    def test_storm_services_empty_list_when_no_storm(self, sup):
        result = sup.check_and_suppress("lone-svc")
        # Only one service → not a storm; storm_services contains just this one
        assert result.storm_service_count == 1
        assert "lone-svc" in result.storm_services

    def test_storm_active_ttl_helper(self, sup):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        assert sup.storm_active_ttl() > 0

    def test_storm_active_ttl_minus2_when_no_storm(self, sup):
        assert sup.storm_active_ttl() == -2

    def test_storm_clears_after_ttl_allows_fresh_service(self, sup, r):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        # storm:active has a 5-minute TTL; ZSET entries age out after 60 seconds.
        # Simulate both expiries to represent the full storm having passed.
        r.delete("suppress:storm:active")
        r.delete("suppress:storm:alerts")
        result = sup.check_and_suppress("brand-new-svc")
        assert result.allowed is True

    def test_manually_set_storm_active_suppresses_alerts(self, sup, r):
        """Pre-existing storm:active key (e.g. set by another instance) suppresses new alerts."""
        r.set("suppress:storm:active", "1", ex=300)
        result = sup.check_and_suppress("svc-x")
        assert result.allowed is False
        assert result.reason == "storm_active"

    def test_clear_storm_removes_active_key(self, sup, r):
        for i in range(6):
            sup.check_and_suppress(f"svc-{i}")
        sup.clear_storm()
        assert not r.exists("suppress:storm:active")
        assert not r.exists("suppress:storm:alerts")


# ═══════════════════════════════════════════════════════════════════════════════
# Interaction between cooldown and storm
# ═══════════════════════════════════════════════════════════════════════════════


class TestCooldownAndStormInteraction:
    def test_cooldown_takes_priority_over_storm_active(self, sup, r):
        """
        A service already in cooldown must be suppressed by cooldown — not storm_active —
        even if a storm is also active.  Cooldown is checked first in the flow.
        """
        sup.check_and_suppress("svc-a")          # sets cooldown
        r.set("suppress:storm:active", "1", ex=300)   # inject storm_active
        result = sup.check_and_suppress("svc-a")
        assert result.reason == "cooldown"        # NOT "storm_active"

    def test_service_not_in_cooldown_is_suppressed_by_storm(self, sup, r):
        """A fresh service encountering an active storm is suppressed by storm_active."""
        r.set("suppress:storm:active", "1", ex=300)
        result = sup.check_and_suppress("fresh-svc")
        assert result.reason == "storm_active"

    def test_service_updates_storm_zset_score_not_duplicated(self, sup, r):
        """
        Re-alerting the same service (after cooldown clears) updates its ZSET score
        rather than adding a duplicate — the service is still counted once.
        """
        sup.check_and_suppress("svc-repeat")
        r.delete("suppress:cooldown:svc-repeat")   # expire cooldown
        sup.check_and_suppress("svc-repeat")       # second alert

        count, services = sup._storm_window_state()
        assert services.count("svc-repeat") == 1   # exactly one entry

    def test_result_fields_always_populated(self, sup):
        """Every SuppressResult must have all fields set regardless of reason."""
        result = sup.check_and_suppress("svc-z")
        assert isinstance(result.allowed, bool)
        assert isinstance(result.reason, str)
        assert isinstance(result.is_storm, bool)
        assert isinstance(result.storm_service_count, int)
        assert isinstance(result.storm_services, list)
