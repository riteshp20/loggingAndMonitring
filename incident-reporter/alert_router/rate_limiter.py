"""Sliding-window rate limiter — max N alerts per service per hour.

The clock is injectable so tests can advance time without sleeping.
Thread-safe via a per-instance lock.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_ALERTS_PER_WINDOW = 10
WINDOW_SECONDS = 3600  # 1 hour


class RateLimiter:
    """Sliding-window rate limiter keyed by service name.

    Inject *_now_fn* in tests to control time:
        limiter = RateLimiter(_now_fn=lambda: fake_clock)
    """

    def __init__(
        self,
        max_alerts: int = MAX_ALERTS_PER_WINDOW,
        window_seconds: int = WINDOW_SECONDS,
        _now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._max = max_alerts
        self._window = window_seconds
        self._now = _now_fn or time.time
        self._windows: dict[str, list[float]] = {}  # service → sorted timestamps
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────────────────────────

    def check_and_record(self, service: str) -> tuple[bool, bool]:
        """Check whether an alert for *service* is allowed.

        Returns ``(allowed, digest_queued)``:
          - ``(True, False)``  — alert is within the window limit; timestamp recorded
          - ``(False, True)``  — window is full; caller should queue to digest
        """
        now = self._now()
        with self._lock:
            window = self._purge(service, now)
            if len(window) < self._max:
                window.append(now)
                self._windows[service] = window
                logger.debug(
                    "Rate check allowed | service=%s count=%d/%d",
                    service, len(window), self._max,
                )
                return True, False
            logger.info(
                "Rate limit reached | service=%s count=%d/%d",
                service, len(window), self._max,
            )
            return False, True

    def current_count(self, service: str) -> int:
        """Number of alerts recorded in the current sliding window."""
        now = self._now()
        with self._lock:
            return len(self._purge(service, now))

    def reset(self, service: str) -> None:
        """Clear the rate-limit window for *service* (operator override)."""
        with self._lock:
            self._windows.pop(service, None)
        logger.info("Rate limit reset | service=%s", service)

    def reset_all(self) -> None:
        """Clear all windows."""
        with self._lock:
            self._windows.clear()

    # ── internal ──────────────────────────────────────────────────────────────

    def _purge(self, service: str, now: float) -> list[float]:
        """Return the unexpired timestamps for *service*, mutating the stored list."""
        cutoff = now - self._window
        window = [t for t in self._windows.get(service, []) if t >= cutoff]
        self._windows[service] = window
        return window
