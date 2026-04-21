# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AI-powered log monitoring agent built to handle 100K+ log events/second from 50–200 microservices. This repository is built in phases; Phase 1 (this directory) covers the ingestion pipeline.

## Commands

```bash
# First-time setup — copies .env, builds images, starts stack, runs smoke tests
bash scripts/setup.sh

# Start / stop
docker compose up -d --build
docker compose down -v          # -v removes volumes (wipes Kafka data + Fluent Bit state)

# Smoke tests against a running stack
bash scripts/test_ingestion.sh

# Tail live Fluent Bit output
docker compose logs -f fluent-bit

# Inspect Kafka messages
docker compose exec kafka \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic raw-logs --from-beginning --max-messages 10

# Rebuild only Fluent Bit (after config edits)
docker compose up -d --build fluent-bit

# Send a one-off test log via TCP
printf '{"level":"ERROR","message":"test","service_name":"my-svc"}\n' | nc 127.0.0.1 5170
```

## Architecture

```
Application logs (files)          Network sources
  /var/log/app/json/*.log   ──┐   TCP :5170 (JSON)
  /var/log/app/plain/*.log  ──┤   UDP :5171 (JSON, lossy)
                              │   Syslog :5140 (RFC5424)
                              ▼
                        ┌──────────────┐
                        │  Fluent Bit  │  Lua normalisation
                        │  (collector) │  + record_modifier
                        └──────┬───────┘
                               │ lz4-compressed JSON
                               ▼
                        Kafka topic: raw-logs
                        (24 partitions, 7-day retention)
```

### Fluent Bit pipeline (fluent-bit/)

Five inputs feed a single filter chain:

| Input | Tag pattern | Purpose |
|---|---|---|
| `tail` JSON | `file.json.<env>.<svc>` | Structured app logs, one JSON per line |
| `tail` plaintext | `file.plain.<env>.<svc>` | Legacy logs, multiline stack-trace reassembly |
| `tcp` | `tcp.<env>.<svc>` | SDK / sidecar JSON over TCP |
| `udp` | `udp.<env>.<svc>` | Fire-and-forget metrics-adjacent logs |
| `syslog` | `syslog.<env>.<svc>` | Infrastructure components |

All inputs use `storage.type filesystem` so chunks survive container restarts and Kafka outages. `storage.pause_on_chunks_overlimit On` applies backpressure to producers rather than dropping data.

Filter chain (order matters):
1. **Lua** (`lua/normalize.lua`) — unifies JSON-sourced and plaintext-sourced records into the canonical schema
2. **record_modifier** — stamps `hostname`, `environment`, `service_name`, `agent_version` (cannot be overwritten by user data)
3. **grep** — drops `DEBUG` records on tags matching `*.prod.*`

### Canonical log schema

Every record reaching Kafka has these fields (empty string when absent):

```
@timestamp   environment  service_name  hostname      agent_version
message      log_level    logger        trace_id      span_id
error        source_file  extra{}
```

`extra` holds any fields not in the reserved set — avoids schema pollution while preserving all data.

### Kafka output

Single topic `raw-logs` with 24 partitions. Key design choices:
- `acks=1` — leader-only acknowledgement for maximum throughput; acceptable for log data
- `Retry_Limit False` — Fluent Bit retries chunks indefinitely (disk-backed)
- `rdkafka.message.timeout.ms 300000` — hard per-message deadline prevents zombie retries
- `compression.codec lz4` — ~4× size reduction, lower CPU than gzip/zstd at this volume
- `batch.num.messages 10000` + `queue.buffering.max.ms 50` — coalesce for throughput without sacrificing latency

Dead-letter topic (`dead-letter-logs`) is pre-created with 30-day retention for reprocessing.

## Key files

| File | Purpose |
|---|---|
| `fluent-bit/fluent-bit.conf` | All inputs, filters, output — read this first |
| `fluent-bit/parsers.conf` | Regex parsers + multiline stack-trace parsers |
| `fluent-bit/lua/normalize.lua` | Schema normalisation; defines canonical field names |
| `docker-compose.yml` | Full local stack with Kafka KRaft + Kafka UI + Fluent Bit |
| `scripts/test_ingestion.sh` | Exercises all 5 input paths + output delivery assertion |

## Environment variables

All required vars are documented in `.env.example`. The three that affect log metadata and Kafka routing are `ENVIRONMENT`, `SERVICE_NAME`, and `HOSTNAME` — they are embedded in every record and in Fluent Bit tags.

## Ports (local)

| Port | Protocol | Service |
|---|---|---|
| 5170 | TCP | Fluent Bit JSON input |
| 5171 | UDP | Fluent Bit JSON input (lossy) |
| 5140 | TCP | Syslog RFC5424 input |
| 2020 | TCP | Fluent Bit HTTP API (`/api/v1/health`, `/api/v1/metrics`) |
| 9092 | TCP | Kafka broker |
| 8080 | TCP | Kafka UI |

---

# Phase 2 — Anomaly Detection Engine

Adaptive anomaly detection service that consumes `ServiceWindowAggregate` feature
vectors from the `processed-logs` Kafka topic and emits `AnomalyEvent` records to
the `anomaly-events` topic.  All state (baselines, suppression, ML models) is
stored in Redis.

## Commands

```bash
# Run the full test suite (113 tests, no external services required)
cd anomaly-detector
pip install -r requirements.txt
python -m pytest tests/ -v

# Start the detector against the running Phase 1 stack
docker compose up -d --build anomaly-detector

# Tail detector logs
docker compose logs -f anomaly-detector

# Inspect emitted anomaly events
docker compose exec kafka \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic anomaly-events --from-beginning --max-messages 5

# Inspect Redis baseline state for a service
docker compose exec redis redis-cli HGETALL "svc:payment-svc:baseline:error_rate"

# Clear storm state manually (operator override)
docker compose exec redis redis-cli DEL suppress:storm:active suppress:storm:alerts
```

## Architecture

```
processed-logs (Kafka)
  ServiceWindowAggregate records
          │
          ▼
  ┌───────────────────────────────────────────────────────┐
  │              AnomalyDetectionPipeline                 │
  │                                                       │
  │  ┌─────────────┐   ┌──────────────────────────────┐  │
  │  │  Phase 1    │   │         Phase 2              │  │
  │  │  Baseline   │──▶│   TwoTierDetector             │  │
  │  │  Manager    │   │   Tier 1: Z-score (< 100ms)  │  │
  │  │  (Redis)    │   │   Tier 2: Isolation Forest   │  │
  │  └─────────────┘   └──────────────┬───────────────┘  │
  │                                   │ is_anomaly=True   │
  │                    ┌──────────────▼───────────────┐  │
  │                    │       Phase 3                │  │
  │                    │   AlertSuppressor             │  │
  │                    │   cooldown 10 min per svc    │  │
  │                    │   storm: >5 svcs / 60 s      │  │
  │                    └──────────────┬───────────────┘  │
  │                                   │ allowed / storm   │
  │                    ┌──────────────▼───────────────┐  │
  │                    │       Phase 4                │  │
  │                    │   AnomalyEventEmitter         │  │
  │                    │   build AnomalyEvent          │  │
  │                    │   publish → anomaly-events    │  │
  │                    └──────────────────────────────┘  │
  └───────────────────────────────────────────────────────┘
          │
          ▼
  anomaly-events (Kafka)
  AnomalyEvent JSON records
```

## Four-phase pipeline

### Phase 1 — Per-service rolling baselines (`baseline/`)

Every numeric metric in the feature vector is tracked individually in Redis.

| Redis key | Type | Purpose |
|---|---|---|
| `svc:{service}:first_seen` | STRING | Unix timestamp; written once (SETNX) |
| `svc:{service}:baseline:{metric}` | HASH | `{mean, std_dev, ewma, sample_count, last_updated}` |
| `svc:{service}:samples:{metric}` | ZSET | Raw samples for 7-day rolling window; score = Unix timestamp |

EWMA formulae (α = 0.1 by default):

```
mean_new = α·x + (1−α)·mean_old                    (EWMA mean)
V_new    = (1−α)·V_old + α·(1−α)·(x − mean_old)²   (EWMA variance)
std_dev  = √V_new
```

**Cold start**: a service is in "learning" mode for 24 hours after `first_seen`.
`is_learning()` returns `True` during this window; the pipeline suppresses all
alerts regardless of z-score.

### Phase 2 — Two-tier detection (`detection/`)

**Tier 1 — Z-score** (every window, target latency < 100 ms)

1. Read the OLD baseline for each metric (before updating).
2. Update the baseline with the new observation.
3. Compute `z = (value − ewma_old) / std_dev_old`.
4. Fire if `max(|z|) > 3.0`.

Using the pre-update baseline ensures the current observation is evaluated against
historical behaviour and does not inflate its own z-score.

**Tier 2 — Isolation Forest** (per service, retrained weekly)

| Redis key | Type | Purpose |
|---|---|---|
| `if_model:{service}:blob` | STRING | joblib-serialised `IsolationForest` |
| `if_model:{service}:training` | ZSET | Feature vectors for retraining; score = timestamp |
| `if_model:{service}:last_trained` | STRING | Unix timestamp of last fit |

- Model fires when `decision_function(x) < −0.15` (calibrated against training
  contamination boundary; robust to feature-scale differences between metrics).
- `maybe_retrain()` runs on every `score()` call but only triggers a fit if
  the model is absent or more than 7 days old — one Redis GET is the common path.
- Minimum 100 samples required before the first fit.

**Verdict logic**

| Condition | `is_anomaly` | `verdict_reason` |
|---|---|---|
| Service in cold-start window | False | `"learning"` |
| Tier 1 max \|z\| > 5.0 | True | `"zscore_critical"` |
| Tier 1 fired AND Tier 2 fired | True | `"both_tiers"` |
| Anything else | False | `"none"` |

### Phase 3 — Suppression and deduplication (`suppression/`)

| Redis key | Type | TTL | Purpose |
|---|---|---|---|
| `suppress:cooldown:{service}` | STRING | 600 s | Per-service 10-minute cooldown |
| `suppress:storm:alerts` | ZSET | rolling 60 s | member = service name, score = timestamp |
| `suppress:storm:active` | STRING | 300 s | SET NX — atomically claimed by storm trigger |

**Cooldown**: the first alert for a service sets the key via `SET NX EX 600`.
Subsequent calls within 10 minutes return `reason="cooldown"`.

**Storm detection**: after recording each alert, distinct service count in the
ZSET is checked.  If `count > 5`, the first caller to `SET NX storm:active`
returns `is_storm=True` (send ONE system-wide incident); all others get
`reason="storm_active"`.  Using the service name as ZSET member means re-alerts
from the same service update the score rather than adding duplicates.

**Check order** (a service hits the first matching rule):
1. Cooldown active → `reason="cooldown"`
2. Storm active → `reason="storm_active"`
3. Record alert → check window count
4. Count > 5 → claim storm → `is_storm=True`
5. Otherwise → `allowed=True`

### Phase 4 — AnomalyEvent emission (`events/`)

**`AnomalyEvent` schema**

```
event_id            UUID4
service             service name; "system" for system_wide_incident
anomaly_type        "zscore_critical" | "both_tiers" | "system_wide_incident"
severity            "P1" | "P2" | "P3"
z_score             max |z| from Tier 1 (0.0 for system_wide_incident)
baseline            {metric: {mean, std_dev, ewma, sample_count, last_updated}}
observed            {metric: value}  — numeric fields from the feature vector
top_log_samples     up to 20 raw log records from the anomalous window
correlated_services services in the storm window
timestamp           ISO-8601 UTC
```

**Severity mapping**

| `anomaly_type` | z-score | Severity |
|---|---|---|
| `system_wide_incident` | any | P1 |
| `zscore_critical` | > 5.0 | P1 |
| `both_tiers` | ≤ 5.0 | P2 |
| fallback | any | P3 |

The Kafka message **key is the service name** (UTF-8 bytes), giving partition
affinity so all events for the same service land on the same partition in order.

## Key files

| File | Purpose |
|---|---|
| `anomaly-detector/baseline/manager.py` | `BaselineManager` — EWMA + rolling window + cold-start |
| `anomaly-detector/baseline/models.py` | `BaselineData` dataclass |
| `anomaly-detector/detection/tier1_zscore.py` | `ZScoreDetector` — fast per-metric z-score |
| `anomaly-detector/detection/tier2_isolation.py` | `IsolationForestDetector` — per-service IF model |
| `anomaly-detector/detection/detector.py` | `TwoTierDetector` — verdict orchestrator |
| `anomaly-detector/suppression/suppressor.py` | `AlertSuppressor` — cooldown + storm dedup |
| `anomaly-detector/events/emitter.py` | `AnomalyEventEmitter` — builds events + publishes to Kafka |
| `anomaly-detector/events/models.py` | `AnomalyEvent` dataclass + serialisation |
| `anomaly-detector/pipeline.py` | `AnomalyDetectionPipeline` — single entry point wiring all phases |
| `anomaly-detector/main.py` | Kafka consumer entry point; reads `processed-logs`, writes `anomaly-events` |
| `anomaly-detector/tests/` | 113 unit tests (fakeredis, no external services needed) |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `KAFKA_BROKERS` | `kafka-1:9092,kafka-2:9092,kafka-3:9092` | Bootstrap servers |
| `KAFKA_INPUT_TOPIC` | `processed-logs` | Source topic (ServiceWindowAggregate records) |
| `KAFKA_ANOMALY_TOPIC` | `anomaly-events` | Sink topic for AnomalyEvent records |
| `KAFKA_CONSUMER_GROUP` | `anomaly-detector` | Consumer group ID |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL for all state |

## Ports (Phase 2)

| Port | Protocol | Service |
|---|---|---|
| 6379 | TCP | Redis (baselines, suppression state, IF models) |

---

# Phase 3 — AI Incident Report Generator

Consumes `AnomalyEvent` records from the `anomaly-events` Kafka topic, enriches
them with live OpenSearch context, scrubs PII, generates a structured incident
report using the Claude API, and stores the result in OpenSearch for downstream
alerting and dashboards.

## Commands

```bash
# Run the full test suite (240 tests, no external services required)
cd incident-reporter
pip install -r requirements.txt
python -m pytest tests/ -v

# Start the incident reporter against the running stack
docker compose up -d --build incident-reporter

# Tail incident reporter logs
docker compose logs -f incident-reporter

# Inspect stored incident reports in OpenSearch
curl -s "http://localhost:9200/incident-reports/_search?pretty&size=5"

# Inspect incoming anomaly events (Phase 2 output / Phase 3 input)
docker compose exec kafka \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic anomaly-events --from-beginning --max-messages 5
```

## Architecture

```
anomaly-events (Kafka)
  AnomalyEvent JSON records
          │
          ▼
  ┌────────────────────────────────────────────────────────────┐
  │               IncidentReportPipeline                       │
  │                                                            │
  │  ┌─────────────────────────────────────────────────────┐  │
  │  │  Stage 1 — EnrichmentService                        │  │
  │  │  (parallel OpenSearch fetches + service registry)   │  │
  │  │  • log_samples   → logs-* index  (±5 min window)   │  │
  │  │  • correlated    → incident-reports (±2 min)        │  │
  │  │  • deployments   → deployments index (last 3)       │  │
  │  │  • oncall_owner  → service_registry.json            │  │
  │  └──────────────────────────┬──────────────────────────┘  │
  │                             │ EnrichedAnomalyEvent         │
  │  ┌──────────────────────────▼──────────────────────────┐  │
  │  │  Stage 2 — PiiScrubber                              │  │
  │  │  Redacts email, credit card, phone, IPv4, IPv6      │  │
  │  │  before any data leaves the service boundary        │  │
  │  └──────────────────────────┬──────────────────────────┘  │
  │                             │ scrubbed EnrichedAnomalyEvent│
  │  ┌──────────────────────────▼──────────────────────────┐  │
  │  │  Stage 3 — ClaudeReportGenerator                    │  │
  │  │  claude-sonnet-4-20250514 · max_tokens=800          │  │
  │  │  Retry (×3, exponential backoff) on transient err   │  │
  │  │  Fallback report (ai_generated=False) when API down │  │
  │  └──────────────────────────┬──────────────────────────┘  │
  │                             │ IncidentReport               │
  │  ┌──────────────────────────▼──────────────────────────┐  │
  │  │  Stage 4 — ReportStore                              │  │
  │  │  Indexes doc into incident-reports (OpenSearch)     │  │
  │  │  doc_id = event_id  (links report ↔ AnomalyEvent)  │  │
  │  │  Store failure is non-fatal — report still returned │  │
  │  └─────────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────────┘
          │
          ▼
  incident-reports (OpenSearch)
  IncidentReport JSON documents
```

## Four-stage pipeline

### Stage 1 — Enrichment (`enrichment/`)

Three OpenSearch queries run in parallel via `ThreadPoolExecutor(max_workers=3)`:

| Query | Index | Window | Purpose |
|---|---|---|---|
| Log samples | `logs-*` | −5 min / +1 min | Raw log lines near the anomaly |
| Correlated anomalies | `incident-reports` | ±2 min | Other services firing at the same time |
| Recent deployments | `deployments` | last 3 docs | Recent changes that may explain the anomaly |

On-call owner is looked up synchronously from `service_registry.json`.  The
`"default"` key is a fallback for services not in the registry.

### Stage 2 — PII scrubbing (`pii/`)

Applied to `log_samples`, `top_log_samples`, `correlated_anomalies`, and
`recent_deployments` **before** the enriched event is passed to Claude.  The
`oncall_owner` block is intentionally not scrubbed (pager addresses must stay
intact).

| Pattern | Replacement |
|---|---|
| Email addresses | `[EMAIL REDACTED]` |
| Credit card numbers (Visa/MC/Amex/Discover) | `[CC REDACTED]` |
| Phone numbers (US formats, E.164) | `[PHONE REDACTED]` |
| IPv4 addresses | `[IPv4 REDACTED]` |
| IPv6 addresses | `[IPv6 REDACTED]` |

`pii_scrub_count` accumulates the total number of substitutions and is stored on
the `IncidentReport` for audit purposes.  The scrubber never mutates its input —
it returns a new `EnrichedAnomalyEvent` via `dataclasses.replace()`.

### Stage 3 — Claude report generation (`report/`)

The generator builds a structured SRE-persona prompt and calls the Anthropic API:

- **Model**: `claude-sonnet-4-20250514`
- **max_tokens**: 800 · **temperature**: 0.2
- **Retry policy**: up to 3 attempts; sleeps `2^attempt` seconds on
  `RateLimitError`, `APIConnectionError`, `APITimeoutError`, `InternalServerError`
- **Fallback**: `AuthenticationError` and `BadRequestError` are not retried.
  Any unrecoverable failure produces a rule-based fallback report with
  `ai_generated=False` and `confidence_score=0.0`.

JSON extraction uses a three-strategy cascade: direct parse → strip markdown
fences → brace-boundary extraction.

### Stage 4 — OpenSearch storage (`storage/`)

`ReportStore.store()` calls `index_document(index="incident-reports", doc_id=report.event_id, ...)`.
Using `event_id` as the document ID makes re-processing idempotent (same event
overwrites the previous report) and lets any component that holds an
`AnomalyEvent` reference look up the corresponding report without a search.

`ensure_index()` is called once on startup and is a no-op if the index already
exists.

## `IncidentReport` schema

| Field | Type | Description |
|---|---|---|
| `report_id` | UUID4 string | Unique report identifier |
| `event_id` | UUID4 string | Links back to the originating `AnomalyEvent` |
| `service` | string | Service that triggered the anomaly |
| `severity` | P1 / P2 / P3 | Inherited from the anomaly event |
| `anomaly_type` | string | `zscore_critical` / `both_tiers` / `system_wide_incident` |
| `timestamp` | ISO-8601 UTC | Anomaly timestamp |
| `generated_at` | ISO-8601 UTC | Report generation timestamp |
| `summary` | string | ≤ 2-sentence human summary |
| `affected_services` | list[str] | All services impacted |
| `probable_cause` | string | Root cause hypothesis |
| `evidence` | list[str] | Supporting data points |
| `suggested_actions` | list[str] | Ordered remediation steps |
| `estimated_user_impact` | string | User-facing impact description |
| `confidence_score` | float 0–1 | AI confidence; 0.0 for fallback reports |
| `ai_generated` | bool | False when Claude was unavailable |
| `oncall_owner` | dict | On-call routing data from service registry |
| `z_score` | float | Max \|z\| from Tier 1 detection |
| `observed_metrics` | dict | Raw metric values that triggered the anomaly |
| `baseline_metrics` | dict | EWMA baseline snapshot at detection time |
| `pii_scrub_count` | int | Total PII substitutions made before Claude call |

## Key files

| File | Purpose |
|---|---|
| `incident-reporter/pipeline.py` | `IncidentReportPipeline` — wires all four stages |
| `incident-reporter/main.py` | Kafka consumer entry point; reads `anomaly-events` |
| `incident-reporter/models.py` | `EnrichedAnomalyEvent` and `EnrichedContext` dataclasses |
| `incident-reporter/enrichment/enricher.py` | `EnrichmentService` — parallel OpenSearch + registry lookup |
| `incident-reporter/enrichment/opensearch_client.py` | All OpenSearch I/O; `with_raw_client()` factory for tests |
| `incident-reporter/enrichment/registry.py` | `ServiceRegistry` — on-call owner lookup from JSON |
| `incident-reporter/pii/scrubber.py` | `PiiScrubber` — immutable scrub with count tracking |
| `incident-reporter/pii/patterns.py` | Compiled regex patterns for all five PII types |
| `incident-reporter/report/claude_client.py` | `ClaudeReportGenerator` — API call, retry, fallback |
| `incident-reporter/report/prompt_builder.py` | System prompt + user message assembly |
| `incident-reporter/report/models.py` | `IncidentReport` dataclass + serialisation |
| `incident-reporter/storage/report_store.py` | `ReportStore` — OpenSearch persistence + index management |
| `incident-reporter/service_registry.json` | On-call routing for 8 services + default fallback |
| `incident-reporter/tests/` | 240 unit tests (mocked I/O, no external services needed) |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `KAFKA_BROKERS` | `kafka-1:9092,kafka-2:9092,kafka-3:9092` | Bootstrap servers |
| `KAFKA_ANOMALY_TOPIC` | `anomaly-events` | Source topic (AnomalyEvent records) |
| `KAFKA_CONSUMER_GROUP` | `incident-reporter` | Consumer group ID |
| `OPENSEARCH_URL` | `http://opensearch:9200` | OpenSearch connection URL |
| `OPENSEARCH_USERNAME` | _(empty)_ | OpenSearch basic-auth username |
| `OPENSEARCH_PASSWORD` | _(empty)_ | OpenSearch basic-auth password |
| `SERVICE_REGISTRY_PATH` | `service_registry.json` | Path to on-call registry JSON |
| `ANTHROPIC_API_KEY` | _(required)_ | Anthropic API key for Claude report generation |

## Ports (Phase 3)

| Port | Protocol | Service |
|---|---|---|
| 9200 | TCP | OpenSearch REST API + document storage |
| 5601 | TCP | OpenSearch Dashboards |

---

# Phase 4 — Alert Routing

Receives `IncidentReport` objects from the Phase 3 pipeline and routes them to
the correct notification channels based on severity, enforcing per-service rate
limits and batching excess alerts into a digest.

## Commands

```bash
# Run the full test suite (597 tests, no external services required)
cd incident-reporter
python -m pytest tests/ -v

# Run only Phase 4 tests
python -m pytest tests/test_routing.py tests/test_slack.py \
  tests/test_pagerduty.py tests/test_rate_limiter.py \
  tests/test_digest.py tests/test_dispatcher.py -v

# Start the incident reporter with alert routing enabled
SLACK_BOT_TOKEN=xoxb-... \
PAGERDUTY_ROUTING_KEY=... \
DASHBOARD_BASE_URL=http://localhost:5601 \
docker compose up -d --build incident-reporter
```

## Architecture

```
IncidentReport (from Phase 3 pipeline)
          │
          ▼
  ┌───────────────────────────────────────────────────────┐
  │                  AlertDispatcher                      │
  │                                                       │
  │  ┌──────────────────────────────────────────────────┐ │
  │  │  Part 1 — AlertRouter                            │ │
  │  │  P1 → PagerDuty + #incidents + email             │ │
  │  │  P2 → #alerts + email                            │ │
  │  │  P3 → #monitoring only                           │ │
  │  │  + service oncall channel appended always        │ │
  │  └─────────────────────┬────────────────────────────┘ │
  │                        │ RoutingDecision               │
  │  ┌─────────────────────▼────────────────────────────┐ │
  │  │  RateLimiter (sliding window, 10 alerts/hr/svc)  │ │
  │  └──────────┬───────────────────────┬───────────────┘ │
  │          allowed               rate-limited            │
  │             │                       │                  │
  │  ┌──────────▼──────────┐  ┌────────▼──────────────┐  │
  │  │  Part 2 — Slack     │  │  DigestBuffer          │  │
  │  │  Block Kit alert    │  │  accumulates excess    │  │
  │  │  + threaded report  │  │  → send_digest() posts │  │
  │  └─────────────────────┘  │    summary to Slack    │  │
  │  ┌──────────────────────┐ └────────────────────────┘  │
  │  │  Part 3 — PagerDuty  │                             │
  │  │  trigger / resolve   │                             │
  │  │  dedup_key = svc +   │                             │
  │  │  anomaly_type        │                             │
  │  └──────────────────────┘                             │
  └───────────────────────────────────────────────────────┘
```

## Four-part implementation

### Part 1 — Severity routing (`alert_router/router.py`)

`AlertRouter.route(report)` produces a `RoutingDecision` from the report's severity:

| Severity | PagerDuty | Slack channels | Email |
|---|---|---|---|
| P1 | ✅ trigger | `#incidents` + service oncall channel | ✅ |
| P2 | ✗ | `#alerts` + service oncall channel | ✅ |
| P3 | ✗ | `#monitoring` + service oncall channel | ✗ |

Unknown severities fall back to P3 rules.  The service-specific on-call channel
from `oncall_owner.slack_channel` is always appended (deduplicated) so the owning
team is always notified.

### Part 2 — Slack Block Kit (`alert_router/slack/`)

`SlackMessageBuilder` assembles two payloads per alert:

**Main alert message** (posted to the channel):
```
[Header]  [P1] payment-svc — Error Rate Spike (+627%)
[Metrics] error_rate: 0.011 → 0.080 *(+627%)*
[Summary] 🤖 AI Summary — DB pool exhausted…
[Errors]  🔴 Top Errors — 1. `DB timeout` ×3  2. `Connection refused`
[Actions] ✅ Acknowledge | 📊 View Dashboard | 🔕 Silence 1hr
[Context] Confidence: 88% | Z-score: 6.2 | AI: ✅ | Report: `abc123…`
```

**Thread reply** (posted as a thread on the main message):
Full incident report — summary, probable cause, evidence, suggested actions,
user impact, on-call routing, and report metadata.

**Digest message** (posted to `#monitoring` when rate-limited alerts are flushed):
Header with suppressed count, list of batched anomalies with severity/z-score/
timestamp, and a context block with the highest z-score and average confidence.

`SlackNotifier.send_alert(channel, report)` posts both messages and returns the
thread timestamp.  `send_message(channel, payload)` posts a raw payload (used
for digest delivery).

### Part 3 — PagerDuty (`alert_router/pagerduty/`)

Uses the **Events API v2** (`https://events.pagerduty.com/v2/enqueue`).

| Operation | When | Payload |
|---|---|---|
| `trigger` | `z_score ≥ 1.5` | summary, severity, source, timestamp, custom_details, runbook link |
| `resolve` | `z_score < 1.5` | routing_key + dedup_key only |

**`dedup_key = "{service}_{anomaly_type}"`** — ties every trigger/resolve for the same
fault together so PagerDuty deduplicates re-alerts automatically.

**Runbook URL** is read from `oncall_owner["runbook_url"]` (populated by the
service registry) and included as a link in the trigger payload.  If absent,
`links` is omitted.

**Severity mapping**: P1 → `critical`, P2 → `error`, P3 → `warning`.

HTTP calls are abstracted behind an injectable `_post_fn` callable — no extra
dependency; the real implementation uses stdlib `urllib`.

### Part 4 — Rate limiting + digest (`alert_router/rate_limiter.py`, `alert_router/digest.py`)

**`RateLimiter`** — sliding-window counter (injectable clock for tests):
- Tracks a list of alert timestamps per service
- Each `check_and_record(service)` prunes timestamps older than 1 hour
- Returns `(True, False)` when count < 10 (allowed)
- Returns `(False, True)` when count ≥ 10 (rate-limited → queue to digest)
- `reset(service)` / `reset_all()` for operator overrides

**`DigestBuffer`** — thread-safe per-service accumulator:
- `add(report)` → appends to buffer, returns new count
- `flush(service)` → returns and clears buffer for one service
- `flush_all()` → returns and clears all services
- `services_with_pending()` → list of services with queued reports

**`AlertDispatcher`** — wires all four parts:
```
dispatch(report):
  1. router.route(report)              → RoutingDecision
  2. rate_limiter.check_and_record()   → allowed | rate_limited
  3a. rate_limited → digest.add(report); return early
  3b. allowed     → pd_client.notify() + slack.send_alert() per target
  4. return DispatchResult

send_digest(service, channel="#monitoring"):
  1. digest.flush(service)
  2. slack.send_message(channel, digest_payload)
```

## `RoutingDecision` fields

| Field | Type | Description |
|---|---|---|
| `report_id` | str | Links to the IncidentReport |
| `service` | str | Service being alerted |
| `severity` | str | P1 / P2 / P3 |
| `channels` | list[Channel] | Channels to notify (pagerduty / slack / email) |
| `slack_targets` | list[str] | Slack channel names to post to |
| `email_recipients` | list[str] | On-call email addresses |
| `send_pagerduty` | bool | Whether to call PagerDuty |
| `send_email` | bool | Whether to send email |
| `rate_limited` | bool | Set True when suppressed by rate limiter |
| `digest_queued` | bool | Set True when added to digest buffer |

## Key files

| File | Purpose |
|---|---|
| `incident-reporter/alert_router/router.py` | `AlertRouter` — severity → `RoutingDecision` |
| `incident-reporter/alert_router/rules.py` | Severity routing rules constant table |
| `incident-reporter/alert_router/models.py` | `Channel` enum, `RoutingDecision` dataclass |
| `incident-reporter/alert_router/rate_limiter.py` | `RateLimiter` — sliding-window, thread-safe |
| `incident-reporter/alert_router/digest.py` | `DigestBuffer` — per-service accumulator |
| `incident-reporter/alert_router/dispatcher.py` | `AlertDispatcher` + `DispatchResult` — full wiring |
| `incident-reporter/alert_router/slack/blocks.py` | `SlackMessageBuilder` — alert + thread + digest payloads |
| `incident-reporter/alert_router/slack/notifier.py` | `SlackNotifier` — posts via Slack Web API |
| `incident-reporter/alert_router/pagerduty/client.py` | `PagerDutyClient` — trigger / resolve / notify |
| `incident-reporter/alert_router/pagerduty/models.py` | `PagerDutyAction`, `PagerDutySeverity` enums |
| `incident-reporter/service_registry.json` | Added `runbook_url` to all 8 services + default |
| `incident-reporter/tests/test_routing.py` | 64 routing tests |
| `incident-reporter/tests/test_slack.py` | 103 Slack Block Kit tests |
| `incident-reporter/tests/test_pagerduty.py` | 84 PagerDuty tests |
| `incident-reporter/tests/test_rate_limiter.py` | 42 rate limiter tests |
| `incident-reporter/tests/test_digest.py` | 38 digest tests |
| `incident-reporter/tests/test_dispatcher.py` | 46 dispatcher tests |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | _(empty — Slack disabled)_ | Slack Bot OAuth token for posting alerts |
| `PAGERDUTY_ROUTING_KEY` | _(empty — PD disabled)_ | PagerDuty Events API v2 routing key |
| `DASHBOARD_BASE_URL` | `http://localhost:5601` | Base URL for "View Dashboard" buttons in Slack |

## Rate limiting behaviour

```
First 10 alerts for a service within any rolling 60-minute window → dispatched normally
Alert 11+ within the same window                                   → suppressed, added to DigestBuffer
After the window slides (oldest alert > 1 hr ago)                 → count decreases, new alerts allowed again
send_digest(service) called by operator/scheduler                  → flushes buffer, posts summary to #monitoring
```

---

# Phase 5 — Dashboard API

FastAPI backend serving the observability dashboard.  Consumes from
`anomaly-events` (Kafka) for the live SSE stream and reads from OpenSearch +
Redis for all query endpoints.

## Commands

```bash
# Run locally (requires OpenSearch + Redis + Kafka reachable)
cd api
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Mint a JWT for testing
python issue_token.py dev-user 24   # subject=dev-user, TTL=24h

# Start via Docker Compose
docker compose up -d --build api

# Hit the health endpoint
curl http://localhost:8000/api/health

# Stream live anomalies (replace TOKEN)
curl -N "http://localhost:8000/api/stream/anomalies?token=TOKEN"

# Tail API logs
docker compose logs -f log-monitor-api
```

## Architecture

```
anomaly-events (Kafka)
        │
        ▼  kafka_consumer_task (background asyncio.Task)
   _FanoutBus.publish()
        │
        ├── Queue (client 1) ──▶ SSE chunks ──▶ browser 1
        └── Queue (client N) ──▶ SSE chunks ──▶ browser N

All other endpoints: AsyncOpenSearch + async Redis (connection pools)
```

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/services` | JWT | List all services with health + silence status |
| `GET` | `/api/services/{id}/metrics` | JWT | 24 h metric history from `incident-reports` index |
| `GET` | `/api/anomalies` | JWT | Paginated anomaly list (`from`, `to`, `svc`, `page`, `size`) |
| `GET` | `/api/reports/{anomaly_id}` | JWT | Fetch AI incident report by event ID or doc ID |
| `GET` | `/api/logs` | JWT | Paginated log search (`service`, `from`, `to`, `level`) |
| `POST` | `/api/alerts/{id}/acknowledge` | JWT | Mark alert acknowledged (stored in Redis) |
| `POST` | `/api/services/{id}/silence` | JWT | Silence alerts for N minutes (Redis TTL key) |
| `GET` | `/api/stream/anomalies` | `?token=` | SSE stream of live anomaly events |
| `GET` | `/api/health` | none | Liveness + readiness probe (OpenSearch + Redis checks) |
| `GET` | `/docs` | none | Auto-generated Swagger UI |

## Non-functionals

- **JWT authentication** — `HTTPBearer`; all endpoints except `/api/health`, `/docs`, `/api/stream/*` require `Authorization: Bearer <token>`
- **Rate limiting** — sliding-window token bucket in Redis (`rl:{sub}` ZSET); 100 req/min per JWT subject by default; Lua script for atomicity
- **Request logging** — `TraceLoggingMiddleware` generates `X-Trace-ID` UUID per request, logs `METHOD /path STATUS Xms trace=…`
- **SSE auth** — `EventSource` cannot set headers; JWT passed as `?token=` query param; rate limiter reads from query on `/api/stream/*` paths
- **Async throughout** — `AsyncOpenSearch` (aiohttp pool, 10 conn/host) + `redis.asyncio` (pool, 20 conn); all route handlers are `async def`
- **Nginx / SSE** — `X-Accel-Buffering: no` response header + 3600 s proxy timeouts configured in Helm Ingress annotations

## Key files

| File | Purpose |
|---|---|
| `api/main.py` | App factory, middleware registration, lifespan (Kafka consumer task) |
| `api/config.py` | Env-var config (OpenSearch, Redis, JWT, Kafka, rate limit) |
| `api/deps.py` | `lru_cache` async client singletons + connection pool setup |
| `api/auth.py` | `verify_token` dependency + `CurrentUser` type alias |
| `api/middleware.py` | `TraceLoggingMiddleware` + `RateLimitMiddleware` (Lua sliding window) |
| `api/routers/services.py` | `GET /api/services` + `GET /api/services/{id}/metrics` |
| `api/routers/anomalies.py` | `GET /api/anomalies` + `POST /api/alerts/{id}/acknowledge` |
| `api/routers/reports.py` | `GET /api/reports/{anomaly_id}` |
| `api/routers/logs.py` | `GET /api/logs` |
| `api/routers/silence.py` | `POST /api/services/{id}/silence` |
| `api/routers/stream.py` | `GET /api/stream/anomalies` — SSE + `_FanoutBus` + `kafka_consumer_task` |
| `api/issue_token.py` | Dev helper to mint signed JWTs |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENSEARCH_URL` | `http://opensearch:9200` | OpenSearch connection |
| `OPENSEARCH_USERNAME` | _(empty)_ | Basic-auth username (omit when security plugin disabled) |
| `OPENSEARCH_PASSWORD` | _(empty)_ | Basic-auth password |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `SERVICE_REGISTRY_PATH` | `/app/service_registry.json` | Path to on-call registry JSON |
| `JWT_SECRET` | _(required)_ | HS256 signing secret — min 32 chars |
| `RATE_LIMIT_REQUESTS` | `100` | Max requests per window per JWT subject |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window size |
| `KAFKA_BROKERS` | `kafka-1:9092,…` | Bootstrap servers for SSE consumer |
| `KAFKA_ANOMALY_TOPIC` | `anomaly-events` | Topic to stream to SSE clients |
| `KAFKA_CONSUMER_GROUP_SSE` | `api-sse` | Consumer group for the SSE background task |

## Ports (Phase 5)

| Port | Protocol | Service |
|---|---|---|
| 8000 | TCP | Dashboard API (HTTP + SSE) |

---

# Infrastructure

## Local development — Docker Compose

`docker-compose.yml` runs the full stack locally.  Every service has
health checks, restart policies, and named volumes.

### Services

| Container | Image | Ports | Purpose |
|---|---|---|---|
| `kafka-{1,2,3}` | bitnami/kafka:3.7 | 9092 (broker-1 only) | 3-node KRaft cluster |
| `kafka-init` | bitnami/kafka:3.7 | — | One-shot topic bootstrap |
| `kafka-ui` | provectuslabs/kafka-ui | 8080 | Broker UI |
| `kafka-connect` | log-agent/kafka-connect | 8083 | S3 sink connector |
| `connect-init` | curlimages/curl | — | One-shot connector deploy |
| `localstack` | localstack/localstack:3.4 | 4566 | S3 emulation |
| `fluent-bit` | log-agent/fluent-bit | 5170/tcp 5171/udp 5140/tcp 2020 | Log collector |
| `opensearch` | opensearchproject/opensearch:2.13.0 | 9200 | Search + storage |
| `opensearch-dashboards` | opensearchproject/opensearch-dashboards:2.13.0 | 5601 | Dashboards |
| `redis` | redis:7.2-alpine | 6379 | Baseline + rate-limit state |
| `flink-jobmanager` | flink:1.18.1 | 8081 | Flink Web UI + REST |
| `flink-taskmanager` | flink:1.18.1 | — | Task execution (4 slots) |
| `flink-job-submit` | log-agent/flink-log-processor | — | One-shot job submission |
| `anomaly-detector` | log-agent/anomaly-detector | — | Phase 2 pipeline |
| `incident-reporter` | log-agent/incident-reporter | — | Phase 3 + 4 pipeline |
| `api` | log-agent/api | 8000 | Phase 5 dashboard API |
| `log-generator` | log-agent/log-generator | — | Synthetic load (`--profile loadtest`) |

### Key compose commands

```bash
# Start everything (omits log-generator by default)
docker compose up -d --build

# Start with synthetic load
docker compose --profile loadtest up -d --build

# Tail a specific service
docker compose logs -f api

# Stop + wipe all volumes
docker compose down -v
```

### Volumes

| Volume | Purpose |
|---|---|
| `kafka_{1,2,3}_data` | Kafka broker log segments |
| `opensearch_data` | OpenSearch indices |
| `redis_data` | Redis persistence (RDB snapshot) |
| `flink_checkpoints` | Flink checkpoint storage |
| `fluent_bit_state` | Fluent Bit chunk state (survives restarts) |
| `app_logs` | Shared log files between log-generator and Fluent Bit |
| `localstack_data` | LocalStack S3 state |

---

## Kubernetes — Helm chart

Production-grade Helm chart at `k8s/helm/log-monitor/`.

### Quick deploy

```bash
# Lint
helm lint k8s/helm/log-monitor

# Dry-run
helm template log-monitor k8s/helm/log-monitor \
  --set secrets.anthropicApiKey=test \
  --set secrets.jwtSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Install / upgrade
helm upgrade --install log-monitor k8s/helm/log-monitor \
  -f k8s/helm/log-monitor/values-prod.yaml \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set secrets.jwtSecret=$JWT_SECRET \
  --atomic --timeout 10m
```

### Chart structure

```
k8s/helm/log-monitor/
├── Chart.yaml
├── values.yaml          ← dev defaults
├── values-prod.yaml     ← prod overrides (larger disks, more replicas)
└── templates/
    ├── _helpers.tpl     ← shared macros (labels, bootstrap URLs)
    ├── namespace.yaml
    ├── secrets.yaml     ← single Secret; all env vars use secretKeyRef
    ├── networkpolicy-default-deny.yaml
    ├── kafka/           StatefulSet · headless+ClusterIP svc · ConfigMap · PDB · NetworkPolicy
    ├── opensearch/      StatefulSet · headless+ClusterIP svc · PDB · NetworkPolicy
    ├── redis/           StatefulSet + headless svc · NetworkPolicy
    ├── flink/           JobManager Deployment · TaskManager Deployment · shared PVC · ConfigMap · NetworkPolicy ×2
    ├── fluent-bit/      DaemonSet · ServiceAccount · ClusterRole · ClusterRoleBinding · NetworkPolicy
    ├── anomaly-detector/ Deployment · HPA (1→5, 70% CPU) · NetworkPolicy
    ├── incident-reporter/ Deployment · NetworkPolicy
    └── api/             Deployment · Service · HPA (2→10, 60% CPU) · Ingress · NetworkPolicy
```

### Key design decisions

- **PodDisruptionBudgets** — Kafka `minAvailable: 2` (tolerates 1 broker down while meeting `min.insync.replicas=2`); OpenSearch `minAvailable: 2`
- **HPA scale behaviour** — API: fast scale-up (3 pods/60 s), slow scale-down (1 pod/60 s, 3 min window); Anomaly Detector: scale-up (2 pods/60 s), scale-down (1 pod/120 s, 5 min window)
- **Network policies** — default-deny-all baseline; each component opens only the exact ports it needs
- **Ingress** — `nginx.ingress.kubernetes.io/proxy-buffering: "off"` + 3600 s timeouts for SSE streams
- **Config rollout** — `checksum/config` and `checksum/secret` pod annotations trigger rolling restarts on ConfigMap/Secret changes
- **`runAsNonRoot: true`** — every pod; OpenSearch init container runs as root only for `vm.max_map_count`

---

## Cloud infrastructure — Terraform (AWS)

Full AWS infrastructure at `terraform/`.  Uses S3 remote state + DynamoDB locking.

### Quick deploy

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # fill in values
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

### Module map

| Module | Resources |
|---|---|
| `kms` | 5 CMKs (s3 · msk · opensearch · elasticache · eks), key rotation enabled |
| `vpc` | 3-AZ VPC · private/public subnets · per-AZ NAT GWs · VPC Flow Logs |
| `eks` | EKS 1.30 · private API endpoint · IMDSv2 · OIDC provider · 2 managed node groups |
| `msk` | MSK 3.6 · 3 brokers · TLS+SASL/SCRAM · storage autoscaling · SCRAM creds in Secrets Manager |
| `opensearch` | OpenSearch 2.13 · VPC mode · 3 dedicated master nodes · IAM auth · CW slow logs |
| `elasticache` | Redis 7.1 · multi-AZ replication group · TLS · auth token in Secrets Manager |
| `s3` | Flink checkpoints bucket + log archive bucket · SSE-KMS · lifecycle tiers · TLS-only policy |
| `iam` | 5 IRSA roles (flink · anomaly-detector · incident-reporter · api · fluent-bit) with scoped policies |

### Key security decisions

- **No public endpoints** — EKS API `endpoint_public_access = false`; MSK, OpenSearch, Redis all in private subnets
- **IMDSv2 only** — `http_tokens = required` on all node launch templates
- **IRSA over node-level IAM** — each pod assumes its own role via OIDC; no shared broad node permissions
- **S3 lifecycle** — Archive: Standard → Glacier IR (30 d) → Deep Archive (365 d); Checkpoints: hard-delete after 7 d
- **MSK SCRAM credentials** — stored in Secrets Manager (KMS-encrypted), never in Terraform state

### Outputs used by Helm

```bash
terraform output -json helm_values
# Returns: kafka_bootstrap, opensearch_url, redis_url,
#          checkpoint_bucket, irsa_* role ARNs
```

---

## CI/CD — GitHub Actions

Four workflows at `.github/workflows/`.

### `pr.yml` — every pull request

| Job | Trigger | What it does |
|---|---|---|
| `secrets-scan` | always | TruffleHog (verified only) + Gitleaks on PR diff |
| `lint` | after secrets-scan | Ruff, Mypy, Checkstyle, `terraform fmt`, `helm lint` |
| `unit-tests` ×3 | after secrets-scan | anomaly-detector (113), incident-reporter (240), Flink Maven |
| `coverage-report` | after unit-tests | Codecov upload + PR comment |
| `integration-tests` | after unit-tests | Real Kafka + OpenSearch + Redis in service containers |
| `docker-build` ×5 | after lint + unit-tests | BuildKit build + Trivy CRITICAL/HIGH scan → SARIF |
| `pr-summary` | after all | Fails PR if any job failed |

### `deploy-staging.yml` — merge to main

1. `changes` — dorny/paths-filter; skips build for unchanged services
2. `build-push` — OIDC → ECR login, BuildKit layer cache, SLSA provenance + SBOM, Trivy scan
3. `deploy-staging` — `helm upgrade --atomic --timeout 10m`, 10-attempt health-check smoke test, Slack on failure

### `deploy-production.yml` — semver tag (`v1.2.3`)

1. `validate-tag` — parses version, verifies all ECR images exist for the tagged SHA
2. `retag-images` — copies ECR manifest by digest (no rebuild); adds version + `latest` tags
3. `deploy-production` — **pauses for manual approval** (GitHub Environment required reviewer), `helm upgrade --atomic`, rollout wait, 12-attempt smoke test, **automatic rollback** on failure
4. `notify` — Slack success/failure + GitHub Release creation

### `secrets-audit.yml` — nightly + every main push

- TruffleHog full history (verified secrets only)
- Gitleaks full history → SARIF → GitHub Security tab
- `pip-audit` per Python service
- OWASP Dependency-Check for Flink (fails on CVSS ≥ 7)
- `tfsec` + Checkov on Terraform → SARIF

### Required GitHub secrets

See `.github/SECRETS.md` for the full inventory and AWS OIDC setup guide.

Key secrets:
- `AWS_ACCOUNT_ID`, `AWS_DEPLOY_ROLE_ARN` — OIDC-based AWS auth (no long-lived keys)
- `AWS_PROD_DEPLOY_ROLE_ARN` — separate role scoped to prod cluster only
- `ANTHROPIC_API_KEY`, `JWT_SECRET_STAGING`, `JWT_SECRET_PROD` — set per-environment
- `SLACK_DEPLOY_WEBHOOK` — deploy notifications

### Key CI/CD decisions

- **OIDC instead of long-lived keys** — no `AWS_ACCESS_KEY_ID` ever stored in GitHub secrets
- **Retag instead of rebuild** — the exact binary tested in staging hits production
- **`--atomic` Helm + explicit rollback** — stores pre-deploy revision; rolls back even if `--atomic` already fired
- **`cancel-in-progress: false`** on deploy jobs — second deploy queues rather than cancels an in-flight rollout
- **Dependabot** — weekly PRs for pip, maven, docker, actions, terraform dependencies
