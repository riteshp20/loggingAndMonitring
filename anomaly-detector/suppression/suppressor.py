"""
Alert suppression and deduplication.

Two mechanisms
──────────────
1. Per-service cooldown (10 minutes)
   Prevents the same service from generating repeat alerts within the cooldown
   window.  The first alert sets a TTL key; subsequent alerts within the TTL
   are silently dropped.

   Redis key:  suppress:cooldown:{service}   STRING "1", TTL = COOLDOWN_SECONDS

2. Storm detection (>5 services within 60 seconds → system-wide incident)
   A ZSET tracks the last alert timestamp for each service.  The member is the
   service name (not a unique ID), so re-alerts from the same service update
   the score rather than adding duplicates — each service is counted once per
   window regardless of how many times it fires.

   When the distinct-service count exceeds STORM_THRESHOLD the detector atomically
   claims the storm slot via SET NX.  The winner emits ONE system-wide incident;
   all subsequent alerts are suppressed until the storm TTL expires.

   Redis key:  suppress:storm:alerts   ZSET  member=service, score=Unix timestamp
   Redis key:  suppress:storm:active   STRING "1", TTL = STORM_COOLDOWN_SECONDS

Flow for check_and_suppress(service)
─────────────────────────────────────
  1. cooldown key exists?          → suppress  reason="cooldown"
  2. storm:active key exists?      → suppress  reason="storm_active"
  3. record alert (set cooldown + ZADD storm ZSET)
  4. count distinct services in 60-second window
     > STORM_THRESHOLD (5)?
       SET NX storm:active → won?  → suppress  reason="storm"  is_storm=True
                            → lost? → suppress  reason="storm_active"
  5. allow individual alert        reason="allowed"

TOCTOU note: the cooldown check and SET are not in one atomic step.
Near-simultaneous identical alerts may both pass through.  For a log monitoring
system occasional duplicates are vastly preferable to missed alerts.
"""

import time

import redis

from .models import SuppressResult

COOLDOWN_SECONDS: int = 600         # 10 minutes
STORM_WINDOW_SECONDS: int = 60      # rolling window for storm detection
STORM_THRESHOLD: int = 5            # >5 distinct services → storm
STORM_COOLDOWN_SECONDS: int = 300   # 5 minutes between storm alerts


class AlertSuppressor:
    _STORM_ALERTS_KEY = "suppress:storm:alerts"
    _STORM_ACTIVE_KEY = "suppress:storm:active"

    def __init__(
        self,
        redis_client: redis.Redis,
        cooldown_seconds: int = COOLDOWN_SECONDS,
        storm_window_seconds: int = STORM_WINDOW_SECONDS,
        storm_threshold: int = STORM_THRESHOLD,
        storm_cooldown_seconds: int = STORM_COOLDOWN_SECONDS,
    ) -> None:
        self._r = redis_client
        self.cooldown_seconds = cooldown_seconds
        self.storm_window_seconds = storm_window_seconds
        self.storm_threshold = storm_threshold
        self.storm_cooldown_seconds = storm_cooldown_seconds

    # ── key helper ────────────────────────────────────────────────────────────

    def _cooldown_key(self, service: str) -> str:
        return f"suppress:cooldown:{service}"

    # ── public API ────────────────────────────────────────────────────────────

    def check_and_suppress(self, service: str) -> SuppressResult:
        """
        Evaluate whether an alert for *service* should be sent.

        Returns SuppressResult where:
          allowed=True              → send the individual alert
          reason="cooldown"         → same service alerted recently; drop this one
          reason="storm_active"     → storm in progress; individual alert suppressed
          reason="storm" is_storm=True → this alert crossed the storm threshold;
                                        caller must send ONE system-wide incident
        """
        # — 1. Per-service cooldown check ─────────────────────────────────────
        if self._r.exists(self._cooldown_key(service)):
            count, services = self._storm_window_state()
            return SuppressResult(
                allowed=False,
                reason="cooldown",
                is_storm=False,
                storm_service_count=count,
                storm_services=services,
            )

        # — 2. Storm-active check (storm already triggered by an earlier alert) ─
        if self._r.exists(self._STORM_ACTIVE_KEY):
            count, services = self._storm_window_state()
            return SuppressResult(
                allowed=False,
                reason="storm_active",
                is_storm=False,
                storm_service_count=count,
                storm_services=services,
            )

        # — 3. Record this alert ───────────────────────────────────────────────
        self._record_alert(service)

        # — 4. Storm threshold check ───────────────────────────────────────────
        count, services = self._storm_window_state()
        if count > self.storm_threshold:
            # SET NX: atomically claim the storm slot.
            # Only one caller wins; the others fall back to "storm_active".
            claimed = self._r.set(
                self._STORM_ACTIVE_KEY,
                "1",
                nx=True,
                ex=self.storm_cooldown_seconds,
            )
            if claimed:
                return SuppressResult(
                    allowed=False,
                    reason="storm",
                    is_storm=True,
                    storm_service_count=count,
                    storm_services=services,
                )
            # Another worker set the key first (concurrent storm trigger).
            return SuppressResult(
                allowed=False,
                reason="storm_active",
                is_storm=False,
                storm_service_count=count,
                storm_services=services,
            )

        # — 5. Allow individual alert ──────────────────────────────────────────
        return SuppressResult(
            allowed=True,
            reason="allowed",
            is_storm=False,
            storm_service_count=count,
            storm_services=services,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _record_alert(self, service: str) -> None:
        """
        Persist the alert event:
        - Set the per-service cooldown key (NX — never overwrites an existing key).
        - Upsert the service in the storm ZSET (member = service name, score = now).
          Using the service name as member means re-alerts update the score rather
          than adding a second entry — each service counts once per window.
        - Prune ZSET entries older than storm_window_seconds.
        """
        now = time.time()
        self._r.set(
            self._cooldown_key(service),
            "1",
            nx=True,
            ex=self.cooldown_seconds,
        )
        pipe = self._r.pipeline(transaction=False)
        pipe.zadd(self._STORM_ALERTS_KEY, {service: now})
        pipe.zremrangebyscore(
            self._STORM_ALERTS_KEY, 0, now - self.storm_window_seconds
        )
        pipe.expire(self._STORM_ALERTS_KEY, self.storm_window_seconds + 3_600)
        pipe.execute()

    def _storm_window_state(self) -> tuple[int, list[str]]:
        """
        Prune and inspect the current storm window.
        Returns (distinct_service_count, service_names_list).
        """
        now = time.time()
        cutoff = now - self.storm_window_seconds

        pipe = self._r.pipeline(transaction=False)
        pipe.zremrangebyscore(self._STORM_ALERTS_KEY, 0, cutoff)
        pipe.zrange(self._STORM_ALERTS_KEY, 0, -1)
        results = pipe.execute()

        raw = results[1]
        services = [m.decode() if isinstance(m, bytes) else m for m in raw]
        return len(services), services

    # ── inspection helpers (useful for monitoring / operator tools) ───────────

    def cooldown_ttl(self, service: str) -> int:
        """Remaining cooldown TTL in seconds.  -2 = not in cooldown."""
        return self._r.ttl(self._cooldown_key(service))

    def storm_active_ttl(self) -> int:
        """Remaining storm active TTL in seconds.  -2 = no active storm."""
        return self._r.ttl(self._STORM_ACTIVE_KEY)

    def clear_storm(self) -> None:
        """Operator override: immediately clear storm state."""
        self._r.delete(self._STORM_ACTIVE_KEY, self._STORM_ALERTS_KEY)
