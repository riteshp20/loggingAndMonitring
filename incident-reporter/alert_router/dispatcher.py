"""AlertDispatcher — wires routing, rate-limiting, and delivery together.

Flow for each IncidentReport:
  1. AlertRouter         → RoutingDecision (channels, targets, PD flag)
  2. RateLimiter         → allowed | rate_limited
  3a. If rate_limited    → DigestBuffer.add(); return early
  3b. If allowed         → dispatch to PagerDuty and/or Slack
  4. Return DispatchResult describing what was done

Digest flushing is a separate explicit call (``send_digest``) so the caller
controls when suppressions are surfaced (e.g. end-of-window scheduler).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from report.models import IncidentReport
from alert_router.models import Channel
from alert_router.router import AlertRouter
from alert_router.rate_limiter import RateLimiter
from alert_router.digest import DigestBuffer
from alert_router.slack.notifier import SlackNotifier
from alert_router.pagerduty.client import PagerDutyClient
from metrics import ALERTS_SENT, ALERTS_SUPPRESSED

logger = logging.getLogger(__name__)

_DIGEST_CHANNEL = "#monitoring"


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch()`` call."""

    report_id: str
    service: str
    severity: str
    channels_dispatched: list[Channel]
    slack_targets_notified: list[str]
    pagerduty_triggered: bool
    rate_limited: bool
    digest_queued: bool
    digest_pending_count: int = 0


class AlertDispatcher:
    """Routes an IncidentReport to the right channels, enforcing rate limits.

    All notifiers are optional — pass ``None`` to disable that channel.
    """

    def __init__(
        self,
        router: AlertRouter,
        rate_limiter: RateLimiter,
        digest: DigestBuffer,
        slack_notifier: Optional[SlackNotifier] = None,
        pd_client: Optional[PagerDutyClient] = None,
    ) -> None:
        self._router = router
        self._rate_limiter = rate_limiter
        self._digest = digest
        self._slack = slack_notifier
        self._pd = pd_client

    # ── public ────────────────────────────────────────────────────────────────

    def dispatch(self, report: IncidentReport) -> DispatchResult:
        """Route and deliver an alert.  Returns a DispatchResult."""
        decision = self._router.route(report)
        allowed, _ = self._rate_limiter.check_and_record(report.service)

        if not allowed:
            count = self._digest.add(report)
            ALERTS_SUPPRESSED.labels(service=report.service, reason="rate_limited").inc()
            logger.warning(
                "Alert rate-limited | service=%s severity=%s digest_pending=%d",
                report.service, report.severity, count,
            )
            return DispatchResult(
                report_id=report.report_id,
                service=report.service,
                severity=report.severity,
                channels_dispatched=[],
                slack_targets_notified=[],
                pagerduty_triggered=False,
                rate_limited=True,
                digest_queued=True,
                digest_pending_count=count,
            )

        # ── PagerDuty ────────────────────────────────────────────────────────
        pagerduty_triggered = False
        if decision.send_pagerduty and self._pd is not None:
            pagerduty_triggered = self._pd.notify(report)
            if pagerduty_triggered:
                ALERTS_SENT.labels(severity=report.severity, channel="pagerduty").inc()

        # ── Slack ─────────────────────────────────────────────────────────────
        slack_notified: list[str] = []
        if self._slack is not None:
            for target in decision.slack_targets:
                ts = self._slack.send_alert(target, report)
                if ts is not None:
                    slack_notified.append(target)
                    ALERTS_SENT.labels(severity=report.severity, channel="slack").inc()

        logger.info(
            "Alert dispatched | service=%s severity=%s pd=%s slack=%s",
            report.service, report.severity, pagerduty_triggered, slack_notified,
        )
        return DispatchResult(
            report_id=report.report_id,
            service=report.service,
            severity=report.severity,
            channels_dispatched=list(decision.channels),
            slack_targets_notified=slack_notified,
            pagerduty_triggered=pagerduty_triggered,
            rate_limited=False,
            digest_queued=False,
        )

    def send_digest(
        self,
        service: str,
        channel: str = _DIGEST_CHANNEL,
    ) -> bool:
        """Flush the digest buffer for *service* and post a summary to *channel*.

        Returns True if a digest was posted, False if the buffer was empty or
        no Slack notifier is configured.
        """
        reports = self._digest.flush(service)
        if not reports:
            logger.debug("Digest flush | service=%s — buffer empty", service)
            return False
        if self._slack is None:
            logger.warning("Digest flush | service=%s — no Slack notifier", service)
            return False

        payload = self._slack._builder.build_digest_message(service, reports)
        ts = self._slack.send_message(channel, payload)
        ok = ts is not None
        logger.info(
            "Digest posted | service=%s channel=%s count=%d ok=%s",
            service, channel, len(reports), ok,
        )
        return ok

    def send_all_digests(self, channel: str = _DIGEST_CHANNEL) -> dict[str, bool]:
        """Flush and post digests for every service with pending reports.

        Returns a dict of ``{service: posted_ok}``.
        """
        services = self._digest.services_with_pending()
        return {svc: self.send_digest(svc, channel) for svc in services}
