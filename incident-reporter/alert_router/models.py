"""Alert router domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Channel(str, Enum):
    PAGERDUTY = "pagerduty"
    SLACK = "slack"
    EMAIL = "email"


@dataclass
class RoutingDecision:
    """Fully-resolved routing decision for a single IncidentReport."""

    report_id: str
    service: str
    severity: str
    channels: list[Channel]
    slack_targets: list[str]
    email_recipients: list[str]
    send_pagerduty: bool
    send_email: bool
    # Rate-limiting state (populated by RateLimiter in Part 4)
    rate_limited: bool = False
    digest_queued: bool = False

    @property
    def is_routed(self) -> bool:
        """True when the decision was not suppressed by rate-limiting."""
        return not self.rate_limited
