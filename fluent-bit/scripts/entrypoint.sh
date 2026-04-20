#!/bin/bash
# Fluent Bit entrypoint: validates environment, creates runtime dirs, then execs.
set -euo pipefail

log() { echo "[entrypoint] $*" >&2; }

# ── Runtime directories ────────────────────────────────────────────────────────
mkdir -p /var/log/fluent-bit/storage

# ── Required variables ─────────────────────────────────────────────────────────
: "${KAFKA_BROKERS:?KAFKA_BROKERS must be set (e.g. kafka:9092)}"
: "${ENVIRONMENT:?ENVIRONMENT must be set (prod|staging|local)}"
: "${SERVICE_NAME:?SERVICE_NAME must be set}"

log "Environment  : ${ENVIRONMENT}"
log "Service      : ${SERVICE_NAME}"
log "Kafka brokers: ${KAFKA_BROKERS}"
log "Log level    : ${FLB_LOG_LEVEL:-info}"

# ── Wait for Kafka to be reachable ─────────────────────────────────────────────
# Fluent Bit will retry on its own, but failing fast with a clear message
# saves minutes of confusion in CI and developer setups.
KAFKA_HOST="${KAFKA_BROKERS%%,*}"          # take the first broker
KAFKA_HOST_ONLY="${KAFKA_HOST%%:*}"
KAFKA_PORT="${KAFKA_HOST##*:}"
KAFKA_PORT="${KAFKA_PORT:-9092}"

MAX_WAIT=60
WAITED=0
until nc -z -w2 "${KAFKA_HOST_ONLY}" "${KAFKA_PORT}" 2>/dev/null; do
    if [ "${WAITED}" -ge "${MAX_WAIT}" ]; then
        log "ERROR: Kafka at ${KAFKA_HOST_ONLY}:${KAFKA_PORT} not reachable after ${MAX_WAIT}s — aborting"
        exit 1
    fi
    log "Waiting for Kafka at ${KAFKA_HOST_ONLY}:${KAFKA_PORT}… (${WAITED}s)"
    sleep 2
    WAITED=$((WAITED + 2))
done
log "Kafka is reachable"

# ── Start Fluent Bit ──────────────────────────────────────────────────────────
exec /fluent-bit/bin/fluent-bit \
    -c /fluent-bit/etc/fluent-bit.conf \
    -l "${FLB_LOG_LEVEL:-info}"
