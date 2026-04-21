"""PiiScrubber — redacts sensitive patterns from log data before it reaches the LLM.

Scrubs four list fields on ``EnrichedAnomalyEvent`` that may carry raw user data:
  - log_samples          (from OpenSearch)
  - top_log_samples      (from the original AnomalyEvent)
  - correlated_anomalies (other service records, may embed sample logs)
  - recent_deployments   (may contain deployer e-mail addresses)

``oncall_owner`` is intentionally NOT scrubbed — it is our own registry data
and the SRE needs the contact details to page the right team.

Audit logging: counts only (never content) are emitted at INFO level so
security teams can verify scrubbing without re-exposing PII.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from .patterns import PATTERNS
from models import EnrichedAnomalyEvent

logger = logging.getLogger(__name__)

# Fields on EnrichedAnomalyEvent that may contain raw text
_SCRUB_FIELDS = ("log_samples", "top_log_samples", "correlated_anomalies", "recent_deployments")


class PiiScrubber:
    """Applies all PII patterns to the text-bearing fields of an ``EnrichedAnomalyEvent``.

    Returns a deep-copied, scrubbed event; the original is never mutated.
    """

    def scrub_event(self, event: EnrichedAnomalyEvent) -> EnrichedAnomalyEvent:
        """Return a scrubbed copy of *event* with ``pii_scrub_count`` updated."""
        counts_by_field: dict[str, int] = {}
        replacements: dict[str, list] = {}

        for field_name in _SCRUB_FIELDS:
            records = getattr(event, field_name)
            scrubbed_records, n = _scrub_records(records)
            replacements[field_name] = scrubbed_records
            counts_by_field[field_name] = n

        total = sum(counts_by_field.values())
        type_counts = _count_by_type(
            [getattr(event, f) for f in _SCRUB_FIELDS]
        )

        if total > 0:
            logger.info(
                "PII scrubbed | event_id=%s service=%s | "
                "log_samples=%d top_log_samples=%d "
                "correlated_anomalies=%d recent_deployments=%d total=%d | "
                "by_type: %s",
                event.event_id,
                event.service,
                counts_by_field.get("log_samples", 0),
                counts_by_field.get("top_log_samples", 0),
                counts_by_field.get("correlated_anomalies", 0),
                counts_by_field.get("recent_deployments", 0),
                total,
                ", ".join(f"{k}={v}" for k, v in type_counts.items() if v > 0),
            )

        return replace(
            event,
            log_samples=replacements["log_samples"],
            top_log_samples=replacements["top_log_samples"],
            correlated_anomalies=replacements["correlated_anomalies"],
            recent_deployments=replacements["recent_deployments"],
            pii_scrub_count=event.pii_scrub_count + total,
        )

    def scrub_text(self, text: str) -> tuple[str, int]:
        """Scrub a single string.  Returns ``(scrubbed_text, replacement_count)``."""
        total = 0
        for pat in PATTERNS:
            text, n = pat.regex.subn(pat.replacement, text)
            total += n
        return text, total


# ── module-level helpers ──────────────────────────────────────────────────────


def _scrub_value(value: Any) -> tuple[Any, int]:
    """Recursively scrub strings inside dicts, lists, and plain strings."""
    if isinstance(value, str):
        scrubbed = value
        total = 0
        for pat in PATTERNS:
            scrubbed, n = pat.regex.subn(pat.replacement, scrubbed)
            total += n
        return scrubbed, total

    if isinstance(value, dict):
        result: dict = {}
        count = 0
        for k, v in value.items():
            clean, n = _scrub_value(v)
            result[k] = clean
            count += n
        return result, count

    if isinstance(value, list):
        result_list: list = []
        count = 0
        for item in value:
            clean, n = _scrub_value(item)
            result_list.append(clean)
            count += n
        return result_list, count

    # int, float, bool, None — not text, nothing to scrub
    return value, 0


def _scrub_records(records: list) -> tuple[list, int]:
    scrubbed, n = _scrub_value(records)
    return scrubbed, n


def _count_by_type(field_data: list) -> dict[str, int]:
    """Count matches per pattern name across all field data (for audit logging)."""
    counts: dict[str, int] = {pat.name: 0 for pat in PATTERNS}
    combined = str(field_data)  # cheap string repr for counting
    for pat in PATTERNS:
        counts[pat.name] = len(pat.regex.findall(combined))
    return counts
