"""AlertRouter — maps an IncidentReport to a RoutingDecision.

Routing is purely a function of severity (P1/P2/P3).  The service-specific
on-call Slack channel from the registry is appended as an additional target
so the owning team always gets notified regardless of severity tier.
"""

from __future__ import annotations

import logging

from report.models import IncidentReport
from alert_router.models import Channel, RoutingDecision
from alert_router.rules import get_rule

logger = logging.getLogger(__name__)


class AlertRouter:
    """Converts an IncidentReport into a fully-resolved RoutingDecision."""

    def route(self, report: IncidentReport) -> RoutingDecision:
        rule = get_rule(report.severity)

        # Severity-driven Slack channels (#incidents / #alerts / #monitoring)
        slack_targets: list[str] = list(rule["slack_channels"])

        # Add the owning team's on-call channel if distinct
        oncall_channel = (report.oncall_owner or {}).get("slack_channel", "")
        if oncall_channel and oncall_channel not in slack_targets:
            slack_targets.append(oncall_channel)

        # Email recipients: on-call address when email is enabled
        email_recipients: list[str] = []
        if rule["send_email"]:
            oncall_email = (report.oncall_owner or {}).get("oncall", "")
            if oncall_email:
                email_recipients.append(oncall_email)

        decision = RoutingDecision(
            report_id=report.report_id,
            service=report.service,
            severity=report.severity,
            channels=list(rule["channels"]),
            slack_targets=slack_targets,
            email_recipients=email_recipients,
            send_pagerduty=rule["send_pagerduty"],
            send_email=rule["send_email"],
        )

        logger.info(
            "Routing decision | service=%s severity=%s channels=%s slack=%s email=%s pd=%s",
            report.service,
            report.severity,
            [c.value for c in decision.channels],
            slack_targets,
            email_recipients,
            decision.send_pagerduty,
        )
        return decision
