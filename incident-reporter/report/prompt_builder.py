"""Builds the system and user prompts sent to Claude for incident analysis."""

from __future__ import annotations

from models import EnrichedAnomalyEvent

# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer (SRE) with deep experience in \
distributed systems, microservices, and production incident management. You \
analyze anomaly detection alerts from a real-time log monitoring system \
processing over 100,000 log events per second across 50–200 microservices.

Your analysis must be:
- Precise: base every conclusion only on the evidence provided.
- Actionable: suggest specific steps an on-call engineer can execute right now.
- Calibrated: confidence_score must reflect genuine uncertainty — do not inflate it.

You MUST respond with ONLY a valid JSON object — no markdown fences, \
no preamble, no trailing text.  The JSON must have exactly these fields:

{
  "summary": "<2 sentences max: what is happening and why it matters>",
  "severity": "<P1|P2|P3>",
  "affected_services": ["<service names impacted>"],
  "probable_cause": "<specific root-cause hypothesis citing concrete evidence>",
  "evidence": ["<data points from the provided context that support the analysis>"],
  "suggested_actions": ["<ordered, concrete remediation steps>"],
  "estimated_user_impact": "<user-facing description of the impact and blast radius>",
  "confidence_score": <float 0.0–1.0>
}\
"""


def get_system_prompt() -> str:
    return _SYSTEM_PROMPT


def build_user_message(event: EnrichedAnomalyEvent) -> str:
    """Assemble the user-turn message with all available context sections."""
    parts = [_section_overview(event), _section_metrics(event)]

    # Prefer OpenSearch log samples; fall back to those carried in the event
    logs = event.log_samples or event.top_log_samples
    if logs:
        source = "OpenSearch" if event.log_samples else "anomaly detector"
        parts.append(_section_log_samples(logs, source=source))

    if event.correlated_anomalies:
        parts.append(_section_correlated_anomalies(event.correlated_anomalies))

    if event.correlated_services:
        parts.append(_section_storm_services(event.correlated_services))

    if event.recent_deployments:
        parts.append(_section_deployments(event.recent_deployments))

    if event.oncall_owner:
        parts.append(_section_oncall(event.oncall_owner))

    return "\n\n".join(parts)


# ── section builders ──────────────────────────────────────────────────────────


def _section_overview(event: EnrichedAnomalyEvent) -> str:
    return (
        "## ANOMALY ALERT\n\n"
        f"**Service:** {event.service}\n"
        f"**Severity:** {event.severity}\n"
        f"**Anomaly Type:** {event.anomaly_type}\n"
        f"**Z-Score:** {event.z_score:.2f}\n"
        f"**Timestamp:** {event.timestamp}"
    )


def _section_metrics(event: EnrichedAnomalyEvent) -> str:
    lines = ["## METRICS: OBSERVED vs BASELINE"]
    if not event.observed:
        lines.append("*(no numeric metrics available)*")
        return "\n".join(lines)

    for metric, value in event.observed.items():
        bl = event.baseline.get(metric, {})
        ewma = bl.get("ewma", "—")
        std = bl.get("std_dev", "—")
        n = bl.get("sample_count", "—")
        ewma_s = f"{ewma:.4f}" if isinstance(ewma, float) else str(ewma)
        std_s = f"{std:.4f}" if isinstance(std, float) else str(std)
        val_s = f"{value:.4f}" if isinstance(value, (int, float)) else str(value)
        lines.append(
            f"- **{metric}**: observed={val_s}  baseline_ewma={ewma_s}"
            f"  std_dev={std_s}  samples={n}"
        )
    return "\n".join(lines)


def _section_log_samples(samples: list, source: str = "OpenSearch") -> str:
    lines = [f"## LOG SAMPLES (source: {source}, showing up to 20)"]
    for i, rec in enumerate(samples[:20], start=1):
        ts = rec.get("@timestamp", rec.get("timestamp", ""))
        level = rec.get("log_level", rec.get("level", ""))
        msg = rec.get("message", rec.get("msg", str(rec)))
        svc = rec.get("service_name", "")
        tag = f" [{svc}]" if svc else ""
        lines.append(f"[{i:02d}] {ts} {level}{tag}: {msg}")
    return "\n".join(lines)


def _section_correlated_anomalies(anomalies: list) -> str:
    lines = ["## CORRELATED ANOMALIES IN OTHER SERVICES (±2 min)"]
    for a in anomalies[:10]:
        svc = a.get("service", "unknown")
        sev = a.get("severity", "")
        atype = a.get("anomaly_type", "")
        ts = a.get("timestamp", "")
        z = a.get("z_score", "")
        z_s = f" z={z:.2f}" if isinstance(z, float) else ""
        lines.append(f"- {svc}: {sev} {atype} at {ts}{z_s}")
    return "\n".join(lines)


def _section_storm_services(services: list) -> str:
    return (
        "## SERVICES IN ALERT STORM WINDOW\n"
        + ", ".join(str(s) for s in services)
    )


def _section_deployments(deployments: list) -> str:
    lines = ["## RECENT DEPLOYMENTS (last 3)"]
    for i, dep in enumerate(deployments[:3], start=1):
        svc = dep.get("service", "")
        ver = dep.get("version", dep.get("tag", "unknown"))
        ts = dep.get("timestamp", "")
        by = dep.get("deployed_by", dep.get("author", ""))
        by_s = f" by {by}" if by else ""
        lines.append(f"{i}. {svc} {ver} deployed {ts}{by_s}")
    return "\n".join(lines)


def _section_oncall(owner: dict) -> str:
    lines = ["## ON-CALL OWNERSHIP"]
    parts: list[str] = []
    if owner.get("team"):
        parts.append(f"Team: {owner['team']}")
    if owner.get("oncall"):
        parts.append(f"Contact: {owner['oncall']}")
    if owner.get("slack_channel"):
        parts.append(f"Slack: {owner['slack_channel']}")
    if owner.get("pagerduty_service_id"):
        parts.append(f"PagerDuty: {owner['pagerduty_service_id']}")
    lines.append(" | ".join(parts))
    return "\n".join(lines)
