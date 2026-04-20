#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════════
# Kafka Connect connector deployment
#
# Idempotent: uses PUT /connectors/{name}/config which creates or replaces.
# Compatible with BusyBox sh (curlimages/curl base image has no bash).
#
# Steps:
#   1. Wait for Kafka Connect REST API to be available
#   2. Create S3 bucket in LocalStack (no-op if already exists)
#   3. Deploy each connector via PUT (create-or-update)
#   4. Poll until all connectors report RUNNING
# ═══════════════════════════════════════════════════════════════════════════════

set -eu

CONNECT_URL="${KAFKA_CONNECT_URL:-http://kafka-connect:8083}"
LOCALSTACK_URL="${LOCALSTACK_URL:-http://localstack:4566}"
S3_BUCKET="${S3_BUCKET:-log-archive}"
CONNECTORS_DIR="${CONNECTORS_DIR:-/connectors}"
MAX_WAIT=180

log() { echo "[connect-init] $*"; }

# ── wait_for: poll a URL until it returns HTTP 2xx ────────────────────────────
wait_for() {
    name="$1"; url="$2"
    waited=0
    log "Waiting for ${name} at ${url} …"
    while ! curl -sf --max-time 5 "${url}" >/dev/null 2>&1; do
        if [ "${waited}" -ge "${MAX_WAIT}" ]; then
            log "ERROR: ${name} not ready after ${MAX_WAIT}s"
            exit 1
        fi
        sleep 3
        waited=$((waited + 3))
    done
    log "${name} is ready"
}

# ── deploy_connector: PUT config, print the connector state on return ─────────
deploy_connector() {
    name="$1"; config_file="$2"
    log "Deploying connector '${name}' from ${config_file} …"
    response=$(curl -sf -X PUT \
        "${CONNECT_URL}/connectors/${name}/config" \
        -H "Content-Type: application/json" \
        --data-binary "@${config_file}" 2>&1) || {
        log "ERROR: failed to deploy '${name}': ${response}"
        exit 1
    }
    log "Connector '${name}' deployed"
}

# ── wait_running: poll connector status until RUNNING or timeout ──────────────
wait_running() {
    name="$1"
    waited=0
    while [ "${waited}" -lt 60 ]; do
        state=$(curl -sf "${CONNECT_URL}/connectors/${name}/status" 2>/dev/null \
            | grep -o '"connector":{[^}]*}' \
            | grep -o '"state":"[^"]*"' \
            | cut -d'"' -f4 || echo "UNKNOWN")

        failed=$(curl -sf "${CONNECT_URL}/connectors/${name}/status" 2>/dev/null \
            | grep -o '"state":"FAILED"' \
            | wc -l | tr -d ' ')

        if [ "${state}" = "RUNNING" ] && [ "${failed}" -eq 0 ]; then
            log "Connector '${name}' is RUNNING"
            return 0
        fi
        log "  '${name}' state=${state}, failed_tasks=${failed} — waiting …"
        sleep 5
        waited=$((waited + 5))
    done
    log "WARNING: '${name}' did not reach RUNNING within 60s (state=${state}) — check Connect logs"
}

# ── 1. Wait for dependencies ──────────────────────────────────────────────────
wait_for "LocalStack S3"  "${LOCALSTACK_URL}/ready"
wait_for "Kafka Connect"  "${CONNECT_URL}/connectors"

# ── 2. Create S3 buckets ──────────────────────────────────────────────────────
# LocalStack accepts unauthenticated bucket creation with path-style addressing.
log "Creating S3 bucket '${S3_BUCKET}' …"
curl -sf -X PUT "${LOCALSTACK_URL}/${S3_BUCKET}" \
    -H "Host: localhost:4566" >/dev/null 2>&1 \
    || log "Bucket may already exist — continuing"
log "Bucket '${S3_BUCKET}' ready"

# Flink checkpoint storage bucket
log "Creating S3 bucket 'checkpoints' …"
curl -sf -X PUT "${LOCALSTACK_URL}/checkpoints" \
    -H "Host: localhost:4566" >/dev/null 2>&1 \
    || log "Bucket 'checkpoints' may already exist — continuing"
log "Bucket 'checkpoints' ready"

# ── 3. Deploy connectors ──────────────────────────────────────────────────────
deploy_connector "s3-sink-raw-logs"       "${CONNECTORS_DIR}/s3-sink-raw-logs.json"
deploy_connector "s3-sink-processed-logs" "${CONNECTORS_DIR}/s3-sink-processed-logs.json"

# ── 4. Verify RUNNING state ───────────────────────────────────────────────────
wait_running "s3-sink-raw-logs"
wait_running "s3-sink-processed-logs"

# ── 5. Print final status ─────────────────────────────────────────────────────
log "Listing all connectors:"
curl -sf "${CONNECT_URL}/connectors?expand=status" 2>/dev/null \
    | grep -o '"name":"[^"]*"\|"state":"[^"]*"' \
    | paste - - \
    | sed 's/"name":"//;s/"\s*"state":"/ → /;s/"//g' \
    | while IFS= read -r line; do log "  ${line}"; done

log "Done."
