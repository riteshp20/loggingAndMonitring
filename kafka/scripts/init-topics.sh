#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kafka Topic + Internal-Topic Bootstrap
#
# Idempotent: safe to run multiple times (uses --if-not-exists).
# Run order matters: internal Connect topics are created before app topics so
# that the Kafka Connect worker can start cleanly with the correct replication
# factors rather than relying on auto-create.
#
# Partition sizing rationale
# ──────────────────────────
# Design point  : 100 000 events/sec sustained, 500 B average event size
# Gross data rate: 100 000 × 500 B = 50 MB/s
#
# Safe write throughput per partition (empirical Kafka rule of thumb):
#   ≤ 5 MB/s write  (leaves headroom for ISR replication × 3 and consumer lag)
#
# raw-logs
#   Minimum : 50 MB/s ÷ 5 MB/s = 10 partitions
#   Chosen  : 24
#     • Supports 24 parallel stream-processor instances (one per partition)
#     • Divisible by 1,2,3,4,6,8,12,24 — easy to right-size consumer counts
#     • 2× headroom for traffic spikes without repartitioning
#
# processed-logs
#   Volume  : ~70 % of raw after DEBUG filter → ~35 MB/s
#   Minimum : 35 ÷ 5 = 7
#   Chosen  : 12  (clean divisor of 24; allows 1:2 processor fan-out ratio)
#
# anomaly-events
#   Volume  : < 1 % of raw (only detected anomalies) → < 1 MB/s
#   Chosen  : 6   (enough for parallel anomaly consumer instances)
#
# alert-notifications
#   Volume  : << 1 % of raw → near-zero
#   Chosen  : 3   (low parallelism acceptable; alert fan-out is cheap)
#
# Replication factor = 3, min.insync.replicas = 2
#   With RF=3 and min.ISR=2 the cluster tolerates one broker failure without
#   losing write availability (acks=-1) or data (at least 2 copies always).
#   RF=2 would halve storage cost but give zero fault tolerance budget.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

BS="${BOOTSTRAP_SERVERS:-kafka-1:9092,kafka-2:9092,kafka-3:9092}"
RF=3          # replication factor — requires a 3-node cluster
MIN_ISR=2     # minimum in-sync replicas for acks=-1 durability guarantee

log()  { echo "[init-topics] $*"; }
fatal(){ echo "[init-topics] FATAL: $*" >&2; exit 1; }

# ── Wait for cluster to be fully elected ─────────────────────────────────────
log "Waiting for Kafka cluster at ${BS}…"
MAX_WAIT=120; WAITED=0
until kafka-broker-api-versions.sh --bootstrap-server "${BS}" >/dev/null 2>&1; do
    [ $WAITED -ge $MAX_WAIT ] && fatal "Cluster not ready after ${MAX_WAIT}s"
    sleep 3; WAITED=$((WAITED + 3))
done
log "Cluster is ready"

# ── Helper ────────────────────────────────────────────────────────────────────
create_topic() {
    local topic=$1 partitions=$2; shift 2
    log "Creating topic '${topic}' (partitions=${partitions}, rf=${RF}) …"
    kafka-topics.sh \
        --bootstrap-server "${BS}" \
        --create --if-not-exists \
        --topic "${topic}" \
        --partitions "${partitions}" \
        --replication-factor "${RF}" \
        "$@"
}

alter_topic() {
    local topic=$1; shift
    log "Configuring topic '${topic}' …"
    kafka-configs.sh \
        --bootstrap-server "${BS}" \
        --alter \
        --entity-type topics \
        --entity-name "${topic}" \
        "$@"
}

# ═══════════════════════════════════════════════════════════════════════════════
# KAFKA CONNECT INTERNAL TOPICS
# Pre-create with explicit settings so Connect doesn't auto-create them with
# the broker default replication factor (which may be 1 on a fresh node).
# ═══════════════════════════════════════════════════════════════════════════════

log "--- Kafka Connect internal topics ---"

# _connect-configs: sequential config log; must have exactly 1 partition.
# cleanup.policy=compact keeps only the latest config entry per connector key.
create_topic _connect-configs 1 \
    --config cleanup.policy=compact \
    --config min.insync.replicas=${MIN_ISR} \
    --config segment.ms=604800000

# _connect-offsets: source connector offset storage; 25 partitions per Confluent
# recommendation for clusters handling many source tasks.
create_topic _connect-offsets 25 \
    --config cleanup.policy=compact \
    --config min.insync.replicas=${MIN_ISR} \
    --config segment.ms=604800000

# _connect-status: connector/task state; 5 partitions is sufficient for most
# deployments (one Connect worker group rarely needs more).
create_topic _connect-status 5 \
    --config cleanup.policy=compact \
    --config min.insync.replicas=${MIN_ISR} \
    --config segment.ms=604800000

# ═══════════════════════════════════════════════════════════════════════════════
# APPLICATION TOPICS
# ═══════════════════════════════════════════════════════════════════════════════

log "--- Application topics ---"

# ── raw-logs ──────────────────────────────────────────────────────────────────
# Highest-volume topic. Receives every log event from Fluent Bit.
# Hot retention: 7 days.  S3 archival starts at hour 1 via the S3 sink connector.
create_topic raw-logs 24 \
    --config min.insync.replicas=${MIN_ISR} \
    --config retention.ms=604800000 \
    --config segment.ms=3600000 \
    --config segment.bytes=536870912 \
    --config max.message.bytes=1048576 \
    --config compression.type=producer \
    --config cleanup.policy=delete \
    --config unclean.leader.election.enable=false \
    --config message.timestamp.type=LogAppendTime

# ── processed-logs ────────────────────────────────────────────────────────────
# Normalised + enriched log events produced by the stream processor.
# DEBUG logs are already filtered; volume is ~70 % of raw-logs.
# S3 archival mirrors raw-logs schedule.
create_topic processed-logs 12 \
    --config min.insync.replicas=${MIN_ISR} \
    --config retention.ms=604800000 \
    --config segment.ms=3600000 \
    --config segment.bytes=536870912 \
    --config max.message.bytes=1048576 \
    --config compression.type=lz4 \
    --config cleanup.policy=delete \
    --config unclean.leader.election.enable=false \
    --config message.timestamp.type=LogAppendTime

# ── anomaly-events ────────────────────────────────────────────────────────────
# Detected anomalies published by the anomaly detector.
# Longer hot retention (30 days): anomaly history is valuable for correlation
# and model retraining; volume is < 1 % of raw-logs so storage cost is trivial.
create_topic anomaly-events 6 \
    --config min.insync.replicas=${MIN_ISR} \
    --config retention.ms=2592000000 \
    --config segment.ms=86400000 \
    --config max.message.bytes=1048576 \
    --config compression.type=lz4 \
    --config cleanup.policy=delete \
    --config unclean.leader.election.enable=false \
    --config message.timestamp.type=LogAppendTime

# ── alert-notifications ───────────────────────────────────────────────────────
# Outbound alert payloads (PagerDuty / Slack / webhook).
# 30-day retention for audit trail.  Very low throughput; 3 partitions are
# sufficient and keep consumer group management simple.
create_topic alert-notifications 3 \
    --config min.insync.replicas=${MIN_ISR} \
    --config retention.ms=2592000000 \
    --config segment.ms=86400000 \
    --config max.message.bytes=1048576 \
    --config compression.type=lz4 \
    --config cleanup.policy=delete \
    --config unclean.leader.election.enable=false \
    --config message.timestamp.type=LogAppendTime

# ── dead-letter-logs ──────────────────────────────────────────────────────────
# Receives records that failed processing or S3 delivery.
# 30-day retention; checked by on-call before purging.
create_topic dead-letter-logs 6 \
    --config min.insync.replicas=${MIN_ISR} \
    --config retention.ms=2592000000 \
    --config segment.ms=86400000 \
    --config max.message.bytes=2097152 \
    --config compression.type=lz4 \
    --config cleanup.policy=delete

# ═══════════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

log "--- Topic summary ---"
kafka-topics.sh --bootstrap-server "${BS}" --list | sort | while read -r t; do
    info=$(kafka-topics.sh --bootstrap-server "${BS}" --describe --topic "${t}" 2>/dev/null \
           | grep "^Topic:" | head -1)
    log "  ${info}"
done

log "Bootstrap complete."
