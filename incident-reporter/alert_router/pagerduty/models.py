"""PagerDuty domain models — actions, severities, severity mapping."""

from __future__ import annotations

from enum import Enum


class PagerDutyAction(str, Enum):
    TRIGGER = "trigger"
    RESOLVE = "resolve"


class PagerDutySeverity(str, Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_SEVERITY_MAP: dict[str, PagerDutySeverity] = {
    "P1": PagerDutySeverity.CRITICAL,
    "P2": PagerDutySeverity.ERROR,
    "P3": PagerDutySeverity.WARNING,
}


def pd_severity(severity: str) -> PagerDutySeverity:
    """Map an alert severity (P1/P2/P3) to a PagerDuty severity level."""
    return _SEVERITY_MAP.get(severity, PagerDutySeverity.WARNING)
