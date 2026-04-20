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
