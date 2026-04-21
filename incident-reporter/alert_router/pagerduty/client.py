"""PagerDutyClient — Events API v2 trigger / resolve.

HTTP is abstracted behind an injectable *_post_fn* callable so tests never
touch the network.  The real implementation uses stdlib urllib — no extra
dependency needed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from report.models import IncidentReport
from alert_router.pagerduty.models import PagerDutyAction, pd_severity

logger = logging.getLogger(__name__)

_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

# z-score below this threshold → anomaly cleared → resolve the PD incident
RESOLVE_Z_SCORE_THRESHOLD = 1.5


# ── dedup key ─────────────────────────────────────────────────────────────────


def dedup_key(service: str, anomaly_type: str) -> str:
    """Stable key that ties every trigger/resolve for the same fault together."""
    return f"{service}_{anomaly_type}"


# ── default HTTP implementation ───────────────────────────────────────────────


def _default_post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ── client ────────────────────────────────────────────────────────────────────


class PagerDutyClient:
    """Sends trigger and resolve events to the PagerDuty Events API v2.

    Inject *_post_fn* in tests:
        post = MagicMock(return_value={"status": "success", "dedup_key": "..."})
        client = PagerDutyClient(routing_key="test", _post_fn=post)
    """

    def __init__(
        self,
        routing_key: str,
        _post_fn: Optional[Callable[[str, dict], dict]] = None,
    ) -> None:
        self._routing_key = routing_key
        self._post_fn = _post_fn if _post_fn is not None else _default_post

    # ── public ────────────────────────────────────────────────────────────────

    def notify(self, report: IncidentReport) -> bool:
        """Trigger if z_score >= threshold; resolve if the anomaly cleared."""
        if report.z_score < RESOLVE_Z_SCORE_THRESHOLD:
            logger.info(
                "Anomaly cleared (z=%.2f < %.1f) | resolving PD incident | service=%s",
                report.z_score, RESOLVE_Z_SCORE_THRESHOLD, report.service,
            )
            return self.resolve(report.service, report.anomaly_type)
        return self.trigger(report)

    def trigger(self, report: IncidentReport) -> bool:
        """Create or update the PagerDuty incident for *report*."""
        payload = self._build_trigger_payload(report)
        ok = self._send(payload)
        if ok:
            logger.info(
                "PagerDuty triggered | dedup_key=%s severity=%s service=%s",
                payload["dedup_key"], payload["payload"]["severity"], report.service,
            )
        return ok

    def resolve(self, service: str, anomaly_type: str) -> bool:
        """Resolve the PagerDuty incident identified by *service* + *anomaly_type*."""
        payload = self._build_resolve_payload(service, anomaly_type)
        ok = self._send(payload)
        if ok:
            logger.info(
                "PagerDuty resolved | dedup_key=%s service=%s",
                payload["dedup_key"], service,
            )
        return ok

    # ── payload builders ──────────────────────────────────────────────────────

    def _build_trigger_payload(self, report: IncidentReport) -> dict[str, Any]:
        oncall = report.oncall_owner or {}
        runbook_url = oncall.get("runbook_url", "")
        links = [{"href": runbook_url, "text": "Runbook"}] if runbook_url else []

        return {
            "routing_key": self._routing_key,
            "event_action": PagerDutyAction.TRIGGER.value,
            "dedup_key": dedup_key(report.service, report.anomaly_type),
            "payload": {
                "summary": report.summary,
                "severity": pd_severity(report.severity).value,
                "source": report.service,
                "timestamp": report.timestamp,
                "custom_details": {
                    "report_id": report.report_id,
                    "anomaly_type": report.anomaly_type,
                    "z_score": report.z_score,
                    "observed_metrics": report.observed_metrics,
                    "probable_cause": report.probable_cause,
                    "affected_services": report.affected_services,
                    "confidence_score": report.confidence_score,
                    "ai_generated": report.ai_generated,
                },
            },
            "links": links,
            "client": "incident-reporter",
        }

    def _build_resolve_payload(self, service: str, anomaly_type: str) -> dict[str, Any]:
        return {
            "routing_key": self._routing_key,
            "event_action": PagerDutyAction.RESOLVE.value,
            "dedup_key": dedup_key(service, anomaly_type),
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _send(self, payload: dict) -> bool:
        try:
            self._post_fn(_EVENTS_URL, payload)
            return True
        except urllib.error.HTTPError as exc:
            logger.error("PagerDuty HTTP %d for dedup_key=%s: %s",
                         exc.code, payload.get("dedup_key"), exc.reason)
            return False
        except Exception as exc:
            logger.error("PagerDuty send failed for dedup_key=%s: %s",
                         payload.get("dedup_key"), exc)
            return False
