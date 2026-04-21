"""
Per-service rolling baseline manager backed by Redis.

Redis key layout
────────────────
  svc:{service}:first_seen           STRING  Unix float; written once via SETNX
  svc:{service}:baseline:{metric}    HASH    {mean, std_dev, ewma, sample_count, last_updated}
  svc:{service}:samples:{metric}     ZSET    score = Unix timestamp; member = "{ts}:{val}:{uid}"

Rolling window
──────────────
  Raw samples are stored in a sorted set and pruned to the last WINDOW_DAYS days on
  every write.  The EWMA state (Hash) is updated incrementally — no full-scan needed.

EWMA formulae (α = ewma_alpha)
──────────────────────────────
  mean_new  = α·x  + (1−α)·mean_old                           (EWMA mean)
  V_new     = (1−α)·V_old + α·(1−α)·(x − mean_old)²          (EWMA variance)
  std_dev   = √V_new

Cold-start suppression
──────────────────────
  A service is "learning" for COLD_START_HOURS after its first observation.
  Callers must check is_learning() before acting on z-scores or anomaly verdicts.
"""

import math
import time
import uuid
from dataclasses import asdict
from typing import Optional

import redis

from .models import BaselineData


class BaselineManager:
    WINDOW_DAYS: int = 7
    COLD_START_HOURS: int = 24

    def __init__(
        self,
        redis_client: redis.Redis,
        ewma_alpha: float = 0.1,
        window_days: int = WINDOW_DAYS,
        cold_start_hours: int = COLD_START_HOURS,
    ) -> None:
        self._r = redis_client
        self.alpha = ewma_alpha
        self.window_seconds = window_days * 86_400
        self.cold_start_seconds = cold_start_hours * 3_600

    # ── key builders ─────────────────────────────────────────────────────────

    def _first_seen_key(self, service: str) -> str:
        return f"svc:{service}:first_seen"

    def _baseline_key(self, service: str, metric: str) -> str:
        return f"svc:{service}:baseline:{metric}"

    def _samples_key(self, service: str, metric: str) -> str:
        return f"svc:{service}:samples:{metric}"

    # ── cold-start ───────────────────────────────────────────────────────────

    def _register_service(self, service: str) -> None:
        """Atomically record first_seen for a new service (SETNX — no-op if exists)."""
        self._r.setnx(self._first_seen_key(service), time.time())

    def is_learning(self, service: str) -> bool:
        """
        Return True while the service is within its cold-start window.
        A service that has never sent data is also considered "learning".
        Callers should suppress anomaly alerts when this returns True.
        """
        raw = self._r.get(self._first_seen_key(service))
        if raw is None:
            return True
        return (time.time() - float(raw)) < self.cold_start_seconds

    # ── baseline read ────────────────────────────────────────────────────────

    def get_baseline(self, service: str, metric: str) -> Optional[BaselineData]:
        """Return the current baseline, or None if no data has been ingested yet."""
        raw = self._r.hgetall(self._baseline_key(service, metric))
        if not raw:
            return None
        return BaselineData(
            mean=float(raw[b"mean"]),
            std_dev=float(raw[b"std_dev"]),
            ewma=float(raw[b"ewma"]),
            sample_count=int(raw[b"sample_count"]),
            last_updated=float(raw[b"last_updated"]),
        )

    # ── baseline write ───────────────────────────────────────────────────────

    def update_baseline(self, service: str, metric: str, value: float) -> BaselineData:
        """
        Ingest one observation and return the updated BaselineData.

        Steps
        1. Register the service (SETNX first_seen).
        2. Append the sample to the 7-day rolling ZSET and prune stale entries.
        3. Update EWMA mean and EWMA variance incrementally.
        4. Persist the new baseline to the Redis Hash with a TTL slightly beyond
           the rolling window (so dead services eventually expire).
        """
        self._register_service(service)
        now = time.time()

        # — rolling-window sample store ──────────────────────────────────────
        member = f"{now:.6f}:{value:.6f}:{uuid.uuid4().hex[:8]}"
        samples_key = self._samples_key(service, metric)
        cutoff = now - self.window_seconds

        pipe = self._r.pipeline(transaction=False)
        pipe.zadd(samples_key, {member: now})
        pipe.zremrangebyscore(samples_key, 0, cutoff)
        pipe.expire(samples_key, self.window_seconds + 3_600)
        pipe.execute()

        # — EWMA update ───────────────────────────────────────────────────────
        existing = self.get_baseline(service, metric)
        alpha = self.alpha

        if existing is None:
            # Bootstrap on the very first sample.
            updated = BaselineData(
                mean=value,
                std_dev=0.0,
                ewma=value,
                sample_count=1,
                last_updated=now,
            )
        else:
            old_ewma = existing.ewma
            old_variance = existing.std_dev ** 2

            # EWMA mean:     μ_t  = α·x  + (1−α)·μ_{t−1}
            new_ewma = alpha * value + (1 - alpha) * old_ewma

            # EWMA variance: V_t  = (1−α)·V_{t−1} + α·(1−α)·(x − μ_{t−1})²
            # Using old_ewma as the centering point keeps the formula self-consistent
            # and avoids a chicken-and-egg dependency on new_ewma.
            new_variance = (
                (1 - alpha) * old_variance
                + alpha * (1 - alpha) * (value - old_ewma) ** 2
            )
            new_std_dev = math.sqrt(new_variance) if new_variance > 0 else 0.0

            # Running arithmetic mean (Welford one-pass, kept alongside EWMA for reference).
            n = existing.sample_count + 1
            new_mean = existing.mean + (value - existing.mean) / n

            updated = BaselineData(
                mean=new_mean,
                std_dev=new_std_dev,
                ewma=new_ewma,
                sample_count=n,
                last_updated=now,
            )

        # — persist ───────────────────────────────────────────────────────────
        baseline_key = self._baseline_key(service, metric)
        self._r.hset(
            baseline_key,
            mapping={
                "mean": updated.mean,
                "std_dev": updated.std_dev,
                "ewma": updated.ewma,
                "sample_count": updated.sample_count,
                "last_updated": updated.last_updated,
            },
        )
        # Expire slightly beyond the window so the Hash outlives its samples.
        self._r.expire(baseline_key, self.window_seconds + 3_600)

        return updated

    # ── window helpers ───────────────────────────────────────────────────────

    def window_sample_count(self, service: str, metric: str) -> int:
        """Return the number of samples currently stored in the 7-day rolling ZSET."""
        return self._r.zcard(self._samples_key(service, metric))

    # ── service status ───────────────────────────────────────────────────────

    def get_service_status(self, service: str) -> dict:
        """
        Return a summary dict for a service:
          {service, learning, first_seen, age_hours, metrics: {metric: BaselineData}}
        """
        raw_first_seen = self._r.get(self._first_seen_key(service))
        first_seen = float(raw_first_seen) if raw_first_seen else None

        metrics: dict[str, dict] = {}
        for key in self._r.scan_iter(f"svc:{service}:baseline:*"):
            metric_name = key.decode().rsplit(":", 1)[-1]
            b = self.get_baseline(service, metric_name)
            if b:
                metrics[metric_name] = asdict(b)

        return {
            "service": service,
            "learning": self.is_learning(service),
            "first_seen": first_seen,
            "age_hours": (time.time() - first_seen) / 3_600 if first_seen else None,
            "metrics": metrics,
        }
