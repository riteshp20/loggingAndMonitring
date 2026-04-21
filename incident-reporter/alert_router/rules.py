"""Severity → channel routing rules.

P1 — immediate page: PagerDuty + Slack #incidents + email
P2 — non-page alert:  Slack #alerts + email
P3 — low-noise:       Slack #monitoring only
"""

from __future__ import annotations

from typing import TypedDict

from alert_router.models import Channel


class _SeverityRule(TypedDict):
    channels: list[Channel]
    slack_channels: list[str]
    send_email: bool
    send_pagerduty: bool


SEVERITY_RULES: dict[str, _SeverityRule] = {
    "P1": {
        "channels": [Channel.PAGERDUTY, Channel.SLACK, Channel.EMAIL],
        "slack_channels": ["#incidents"],
        "send_email": True,
        "send_pagerduty": True,
    },
    "P2": {
        "channels": [Channel.SLACK, Channel.EMAIL],
        "slack_channels": ["#alerts"],
        "send_email": True,
        "send_pagerduty": False,
    },
    "P3": {
        "channels": [Channel.SLACK],
        "slack_channels": ["#monitoring"],
        "send_email": False,
        "send_pagerduty": False,
    },
}

# Fallback for any unrecognised severity — treated as P3
_DEFAULT_RULE = SEVERITY_RULES["P3"]


def get_rule(severity: str) -> _SeverityRule:
    return SEVERITY_RULES.get(severity, _DEFAULT_RULE)
