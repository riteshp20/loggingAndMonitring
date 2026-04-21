"""DigestBuffer — collects rate-limited reports for batched delivery.

Thread-safe accumulator keyed by service name.  The dispatcher calls
``add()`` for every suppressed alert and ``flush()`` / ``flush_all()``
when it is time to post the digest (e.g. end of rate-limit window, or
on a scheduled tick).
"""

from __future__ import annotations

import logging
import threading

from report.models import IncidentReport

logger = logging.getLogger(__name__)


class DigestBuffer:
    """Thread-safe per-service accumulator for suppressed IncidentReports."""

    def __init__(self) -> None:
        self._buffer: dict[str, list[IncidentReport]] = {}
        self._lock = threading.Lock()

    def add(self, report: IncidentReport) -> int:
        """Append *report* to the digest for its service.

        Returns the new pending count for that service.
        """
        with self._lock:
            buf = self._buffer.setdefault(report.service, [])
            buf.append(report)
            count = len(buf)
        logger.debug("Digest add | service=%s pending=%d", report.service, count)
        return count

    def flush(self, service: str) -> list[IncidentReport]:
        """Remove and return all buffered reports for *service*."""
        with self._lock:
            reports = self._buffer.pop(service, [])
        logger.info("Digest flush | service=%s count=%d", service, len(reports))
        return reports

    def flush_all(self) -> dict[str, list[IncidentReport]]:
        """Remove and return all buffered reports for every service."""
        with self._lock:
            result = {svc: list(rpts) for svc, rpts in self._buffer.items()}
            self._buffer.clear()
        return result

    def pending_count(self, service: str) -> int:
        """Number of buffered reports for *service*."""
        with self._lock:
            return len(self._buffer.get(service, []))

    def total_pending(self) -> int:
        """Total number of buffered reports across all services."""
        with self._lock:
            return sum(len(v) for v in self._buffer.values())

    def services_with_pending(self) -> list[str]:
        """Services that have at least one buffered report."""
        with self._lock:
            return [s for s, r in self._buffer.items() if r]
