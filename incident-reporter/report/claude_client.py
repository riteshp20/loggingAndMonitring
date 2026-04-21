"""Claude API client — generates IncidentReport with retry/backoff and fallback."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import anthropic

from .models import IncidentReport
from .prompt_builder import build_user_message, get_system_prompt
from models import EnrichedAnomalyEvent

if TYPE_CHECKING:
    from pii.scrubber import PiiScrubber

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-20250514"
_MAX_TOKENS = 800
_TEMPERATURE = 0.2
_MAX_RETRIES = 3

# Errors worth retrying: transient network/capacity issues
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ClaudeReportGenerator:
    """Calls Claude to produce a structured SRE incident report.

    Inject *_client* in tests to avoid real API calls.
    Pass *pii_scrubber* to redact PII from log data before it reaches the LLM.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        _client: Optional[anthropic.Anthropic] = None,
        pii_scrubber: Optional["PiiScrubber"] = None,
    ) -> None:
        if _client is not None:
            self._client = _client
        else:
            self._client = anthropic.Anthropic(
                api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
            )
        self._scrubber = pii_scrubber

    # ── public ────────────────────────────────────────────────────────────

    def generate(self, event: EnrichedAnomalyEvent) -> IncidentReport:
        """Generate an AI report for *event*.  Falls back to raw metrics on failure.

        If a ``PiiScrubber`` was provided at construction time, it is applied to
        the event's log fields BEFORE the prompt is built — raw PII never reaches
        the Anthropic API.
        """
        if self._scrubber is not None:
            event = self._scrubber.scrub_event(event)

        system = get_system_prompt()
        user = build_user_message(event)
        try:
            raw_text = self._call_with_retry(system, user)
            data = _extract_json(raw_text)
            report = self._build_report(event, data, ai_generated=True)
            logger.info(
                "AI report generated for event=%s service=%s confidence=%.2f",
                event.event_id, event.service, report.confidence_score,
            )
            return report
        except Exception as exc:
            logger.error(
                "Claude API failed for event=%s service=%s — using fallback: %s",
                event.event_id, event.service, exc,
            )
            return self._fallback_report(event)

    # ── internal ──────────────────────────────────────────────────────────

    def _call_with_retry(self, system: str, user: str) -> str:
        """Call the Claude API with exponential backoff.

        Retries up to _MAX_RETRIES times on transient errors.
        Raises immediately on auth/bad-request errors (no point retrying).
        """
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.messages.create(
                    model=_MODEL,
                    max_tokens=_MAX_TOKENS,
                    temperature=_TEMPERATURE,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return response.content[0].text
            except (anthropic.AuthenticationError, anthropic.BadRequestError):
                raise  # configuration errors — retrying won't help
            except _RETRYABLE as exc:
                last_exc = exc
                delay = 2 ** attempt  # 1 s → 2 s → 4 s
                logger.warning(
                    "Claude API attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1, _MAX_RETRIES, type(exc).__name__, delay,
                )
                time.sleep(delay)
        raise last_exc

    def _build_report(
        self, event: EnrichedAnomalyEvent, data: dict, ai_generated: bool
    ) -> IncidentReport:
        return IncidentReport(
            report_id=str(uuid.uuid4()),
            event_id=event.event_id,
            service=event.service,
            severity=data.get("severity", event.severity),
            anomaly_type=event.anomaly_type,
            timestamp=event.timestamp,
            generated_at=_utc_now(),
            summary=str(data.get("summary", "")),
            affected_services=list(data.get("affected_services", [event.service])),
            probable_cause=str(data.get("probable_cause", "")),
            evidence=list(data.get("evidence", [])),
            suggested_actions=list(data.get("suggested_actions", [])),
            estimated_user_impact=str(data.get("estimated_user_impact", "")),
            confidence_score=_clamp(float(data.get("confidence_score", 0.0))),
            ai_generated=ai_generated,
            oncall_owner=event.oncall_owner,
            z_score=event.z_score,
            observed_metrics=event.observed,
            baseline_metrics=event.baseline,
            pii_scrub_count=event.pii_scrub_count,
        )

    def _fallback_report(self, event: EnrichedAnomalyEvent) -> IncidentReport:
        """Return a minimal report built from raw metrics when Claude is unavailable."""
        evidence = [
            f"{metric}: observed={val}"
            for metric, val in event.observed.items()
        ]
        affected = [event.service] + [
            s for s in event.correlated_services if s != event.service
        ]
        return IncidentReport(
            report_id=str(uuid.uuid4()),
            event_id=event.event_id,
            service=event.service,
            severity=event.severity,
            anomaly_type=event.anomaly_type,
            timestamp=event.timestamp,
            generated_at=_utc_now(),
            summary=(
                f"Anomaly detected in {event.service}: {event.anomaly_type} "
                f"(z={event.z_score:.2f}). AI analysis unavailable — review raw metrics."
            ),
            affected_services=affected,
            probable_cause="Unable to determine — Claude API unavailable.",
            evidence=evidence,
            suggested_actions=[
                "Check service logs for errors and stack traces.",
                "Review recent deployments for this service.",
                "Contact on-call engineer immediately if severity is P1.",
                "Monitor error rate and latency for recovery or escalation.",
            ],
            estimated_user_impact=(
                "Unknown — AI analysis unavailable. "
                "Treat as high-impact until a human assesses the situation."
            ),
            confidence_score=0.0,
            ai_generated=False,
            oncall_owner=event.oncall_owner,
            z_score=event.z_score,
            observed_metrics=event.observed,
            baseline_metrics=event.baseline,
            pii_scrub_count=event.pii_scrub_count,
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict:
    """Extract a JSON dict from Claude's response.

    Handles three shapes:
      1. Pure JSON string
      2. JSON inside ```json ... ``` fences
      3. First ``{`` … last ``}`` block within surrounding prose
    """
    text = text.strip()

    # Direct parse — most common when the model follows instructions
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        if start != end:
            inner = text[start + 3 : end].lstrip("json").strip()
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass

    # Brace extraction (last resort)
    brace_start = text.find("{")
    brace_end = text.rfind("}") + 1
    if 0 <= brace_start < brace_end:
        try:
            return json.loads(text[brace_start:brace_end])
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"No valid JSON found in Claude response (first 200 chars): {text[:200]!r}"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))
