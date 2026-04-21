"""SlackNotifier — posts alert + threaded full report to a Slack channel."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from report.models import IncidentReport
from alert_router.slack.blocks import SlackMessageBuilder

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Sends a Block Kit alert to a Slack channel and posts the full report as a thread.

    Inject *_client* in tests to avoid real Slack API calls.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        dashboard_base_url: str = "http://localhost:5601",
        _client: Any = None,
    ) -> None:
        if _client is not None:
            self._client = _client
        else:
            from slack_sdk import WebClient  # deferred so tests don't need the import
            self._client = WebClient(token=token or os.getenv("SLACK_BOT_TOKEN", ""))
        self._builder = SlackMessageBuilder(dashboard_base_url=dashboard_base_url)

    def send_message(self, channel: str, payload: dict) -> Optional[str]:
        """Post a raw Block Kit payload to *channel* (no thread). Returns ts or None."""
        try:
            resp = self._client.chat_postMessage(channel=channel, **payload)
            return resp["ts"]
        except Exception as exc:
            logger.error("Slack send_message failed | channel=%s error=%s", channel, exc)
            return None

    def send_alert(self, channel: str, report: IncidentReport) -> Optional[str]:
        """Post alert to *channel*, then thread the full report.

        Returns the message timestamp (thread_ts) on success, or None on error.
        """
        try:
            alert_payload = self._builder.build_alert_message(report)
            resp = self._client.chat_postMessage(channel=channel, **alert_payload)
            ts: str = resp["ts"]

            thread_payload = self._builder.build_thread_reply(report)
            self._client.chat_postMessage(channel=channel, thread_ts=ts, **thread_payload)

            logger.info(
                "Slack alert sent | channel=%s service=%s severity=%s ts=%s",
                channel, report.service, report.severity, ts,
            )
            return ts
        except Exception as exc:
            logger.error(
                "Slack send failed | channel=%s service=%s error=%s",
                channel, report.service, exc,
            )
            return None
