"""Slack Block Kit message builders for incident alerts.

build_alert_message() → main channel post (header, metrics, summary, errors, buttons)
build_thread_reply()  → threaded full report
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from report.models import IncidentReport
from alert_router.rate_limiter import MAX_ALERTS_PER_WINDOW

# ── label tables ──────────────────────────────────────────────────────────────

_METRIC_LABELS: dict[str, str] = {
    "error_rate": "Error Rate",
    "p99_latency_ms": "P99 Latency",
    "latency_p99": "P99 Latency",
    "latency_p50": "P50 Latency",
    "request_rate": "Request Rate",
    "throughput": "Throughput",
    "cpu_usage": "CPU Usage",
    "memory_usage": "Memory Usage",
    "5xx_rate": "5xx Rate",
    "4xx_rate": "4xx Rate",
}

_ANOMALY_LABELS: dict[str, str] = {
    "zscore_critical": "Spike",
    "both_tiers": "Anomaly",
    "system_wide_incident": "System-Wide Incident",
}

# ── header helpers ────────────────────────────────────────────────────────────


def _pct_change(observed: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (observed - baseline) / abs(baseline) * 100


def _primary_metric_and_pct(report: IncidentReport) -> tuple[str, float | None]:
    """Return (human-readable metric label, % change) for the most deviated metric."""
    best_metric = ""
    best_pct: float | None = None

    for metric, obs_val in (report.observed_metrics or {}).items():
        baseline_data = (report.baseline_metrics or {}).get(metric, {})
        ewma = baseline_data.get("ewma") if isinstance(baseline_data, dict) else None
        if ewma is not None:
            pct = _pct_change(float(obs_val), float(ewma))
            if pct is not None and (best_pct is None or abs(pct) > abs(best_pct)):
                best_pct = pct
                best_metric = metric

    label = _METRIC_LABELS.get(best_metric, best_metric.replace("_", " ").title())
    return label, best_pct


def header_text(report: IncidentReport) -> str:
    """Return the plain-text header string (also used as notification fallback)."""
    if report.anomaly_type == "system_wide_incident":
        return f"[{report.severity}] System-Wide Incident — Multiple Services Affected"

    metric_label, pct = _primary_metric_and_pct(report)
    anomaly_label = _ANOMALY_LABELS.get(report.anomaly_type, "Anomaly")

    if pct is not None:
        sign = "+" if pct >= 0 else ""
        return f"[{report.severity}] {report.service} — {metric_label} {anomaly_label} ({sign}{pct:.0f}%)"

    return f"[{report.severity}] {report.service} — {metric_label} {anomaly_label}"


# ── individual block builders ─────────────────────────────────────────────────


def _header_block(report: IncidentReport) -> dict[str, Any]:
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": header_text(report), "emoji": True},
    }


def _divider_block() -> dict[str, Any]:
    return {"type": "divider"}


def _metrics_block(report: IncidentReport) -> dict[str, Any]:
    """Two-column section: Metric | Baseline → Observed (±%)."""
    fields: list[dict[str, Any]] = [
        {"type": "mrkdwn", "text": "*Metric*"},
        {"type": "mrkdwn", "text": "*Baseline → Observed*"},
    ]

    for metric, obs_val in list((report.observed_metrics or {}).items())[:4]:
        baseline_data = (report.baseline_metrics or {}).get(metric, {})
        ewma = baseline_data.get("ewma") if isinstance(baseline_data, dict) else None
        label = _METRIC_LABELS.get(metric, metric.replace("_", " ").title())

        if ewma is not None:
            pct = _pct_change(float(obs_val), float(ewma))
            if pct is not None:
                sign = "+" if pct >= 0 else ""
                pct_str = f" *({sign}{pct:.0f}%)*"
            else:
                pct_str = ""
            value_str = f"{ewma:.4g} → {obs_val:.4g}{pct_str}"
        else:
            value_str = f"— → {obs_val:.4g}"

        fields.append({"type": "mrkdwn", "text": label})
        fields.append({"type": "mrkdwn", "text": value_str})

    if len(fields) == 2:
        # No metrics available
        fields.append({"type": "mrkdwn", "text": "—"})
        fields.append({"type": "mrkdwn", "text": "No metric data"})

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*📊 Metrics*"},
        "fields": fields,
    }


def _summary_block(report: IncidentReport) -> dict[str, Any]:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*🤖 AI Summary*\n{report.summary}"},
    }


def _top_errors_block(report: IncidentReport, n: int = 3) -> dict[str, Any] | None:
    """Top-n error messages by frequency from top_log_samples, or None if empty."""
    samples = list(getattr(report, "top_log_samples", None) or [])
    if not samples:
        return None

    counts: Counter[str] = Counter()
    for sample in samples:
        msg = (sample.get("message", "") if isinstance(sample, dict) else str(sample)).strip()
        if msg:
            counts[msg] += 1

    if not counts:
        return None

    lines: list[str] = []
    for i, (msg, count) in enumerate(counts.most_common(n), start=1):
        truncated = msg[:80] + "…" if len(msg) > 80 else msg
        suffix = f" ×{count}" if count > 1 else ""
        lines.append(f"{i}. `{truncated}`{suffix}")

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*🔴 Top Errors*\n" + "\n".join(lines)},
    }


def _actions_block(
    report: IncidentReport,
    dashboard_base_url: str,
) -> dict[str, Any]:
    rid = report.report_id
    dashboard_url = (
        f"{dashboard_base_url.rstrip('/')}/app/discover"
        f"#/?service={report.service}&report_id={rid}"
    )
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Acknowledge", "emoji": True},
                "action_id": f"ack_{rid}",
                "value": f"ack_{rid}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📊 View Dashboard", "emoji": True},
                "action_id": f"dashboard_{rid}",
                "url": dashboard_url,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔕 Silence 1hr", "emoji": True},
                "action_id": f"silence_{rid}",
                "value": f"silence_{rid}",
                "style": "danger",
            },
        ],
    }


def _context_block(report: IncidentReport) -> dict[str, Any]:
    ai_flag = "✅" if report.ai_generated else "⚠️ fallback"
    short_id = report.report_id[:8]
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Confidence: {report.confidence_score:.0%} | "
                    f"Z-score: {report.z_score:.1f} | "
                    f"AI: {ai_flag} | "
                    f"PII scrubs: {report.pii_scrub_count} | "
                    f"Report: `{short_id}…`"
                ),
            }
        ],
    }


# ── thread reply ──────────────────────────────────────────────────────────────


def _thread_blocks(report: IncidentReport) -> list[dict[str, Any]]:
    """Full incident report formatted as Block Kit sections for the Slack thread."""
    blocks: list[dict[str, Any]] = []

    # Title
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":memo: *Full Incident Report — {report.service} ({report.severity})*",
        },
    })
    blocks.append(_divider_block())

    # Summary + probable cause
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*Summary*\n{report.summary}\n\n"
                f"*Probable Cause*\n{report.probable_cause}"
            ),
        },
    })

    # Evidence
    if report.evidence:
        evidence_text = "\n".join(f"• {e}" for e in report.evidence)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Evidence*\n{evidence_text}"},
        })

    # Suggested actions
    if report.suggested_actions:
        action_text = "\n".join(
            f"{i}. {a}" for i, a in enumerate(report.suggested_actions, start=1)
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Suggested Actions*\n{action_text}"},
        })

    # User impact
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Estimated User Impact*\n{report.estimated_user_impact}",
        },
    })

    # On-call routing
    oncall = report.oncall_owner or {}
    pd_id = oncall.get("pagerduty_service_id") or "—"
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*On-Call*\n"
                f"Team: {oncall.get('team', '—')} | "
                f"Slack: {oncall.get('slack_channel', '—')} | "
                f"PagerDuty: `{pd_id}`"
            ),
        },
    })

    blocks.append(_divider_block())

    # Metadata footer
    ai_flag = "✅ Yes" if report.ai_generated else "⚠️ No (fallback)"
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Report: `{report.report_id}` | "
                    f"Event: `{report.event_id}` | "
                    f"Confidence: {report.confidence_score:.0%} | "
                    f"AI Generated: {ai_flag} | "
                    f"PII Scrubs: {report.pii_scrub_count} | "
                    f"Generated: {report.generated_at}"
                ),
            }
        ],
    })

    return blocks


# ── public builder ────────────────────────────────────────────────────────────


class SlackMessageBuilder:
    """Assembles Slack Block Kit payloads from an IncidentReport."""

    def __init__(self, dashboard_base_url: str = "http://localhost:5601") -> None:
        self._dashboard_base_url = dashboard_base_url

    def build_alert_message(self, report: IncidentReport) -> dict[str, Any]:
        """Main alert message payload (blocks + notification fallback text)."""
        blocks: list[dict[str, Any]] = [
            _header_block(report),
            _divider_block(),
            _metrics_block(report),
            _divider_block(),
            _summary_block(report),
        ]

        errors = _top_errors_block(report)
        if errors:
            blocks.append(_divider_block())
            blocks.append(errors)

        blocks.append(_divider_block())
        blocks.append(_actions_block(report, self._dashboard_base_url))
        blocks.append(_context_block(report))

        return {"blocks": blocks, "text": header_text(report)}

    def build_thread_reply(self, report: IncidentReport) -> dict[str, Any]:
        """Full report payload for the threaded reply."""
        return {
            "blocks": _thread_blocks(report),
            "text": f"Full incident report for {report.service} ({report.severity})",
        }

    def build_digest_message(
        self, service: str, reports: list[IncidentReport]
    ) -> dict[str, Any]:
        """Digest payload summarising suppressed (rate-limited) alerts."""
        count = len(reports)
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📦 Alert Digest — {service} ({count} suppressed alert{'s' if count != 1 else ''})",
                    "emoji": True,
                },
            },
            _divider_block(),
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Rate limit reached* — max {MAX_ALERTS_PER_WINDOW} alerts/hr. "
                        f"The following anomalies were suppressed during the rate-limit window."
                    ),
                },
            },
            _divider_block(),
        ]

        lines: list[str] = []
        for r in reports:
            ts_short = r.timestamp[:19].replace("T", " ")
            summary_short = r.summary[:60] + "…" if len(r.summary) > 60 else r.summary
            lines.append(
                f"• [{r.severity}] `{r.anomaly_type}` z={r.z_score:.1f} — "
                f"{summary_short} — _{ts_short}_"
            )

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })

        if reports:
            max_z = max(r.z_score for r in reports)
            avg_conf = sum(r.confidence_score for r in reports) / count
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"Highest z-score: {max_z:.1f} | "
                        f"Avg confidence: {avg_conf:.0%} | "
                        f"Suppressed: {count}"
                    ),
                }],
            })

        return {
            "blocks": blocks,
            "text": f"Alert digest for {service}: {count} suppressed alert{'s' if count != 1 else ''}",
        }
