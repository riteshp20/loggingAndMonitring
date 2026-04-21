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
