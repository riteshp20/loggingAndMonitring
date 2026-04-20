#!/bin/bash
# Fluent Bit health check — used by Docker HEALTHCHECK and Kubernetes liveness probes.
# Exits 0 (healthy) only when the HTTP server reports "ok" AND
# output error counters haven't spiked beyond the configured threshold.
set -euo pipefail

FB_HOST="${FLUENT_BIT_HOST:-127.0.0.1}"
FB_PORT="${FLUENT_BIT_HTTP_PORT:-2020}"
MAX_OUTPUT_ERRORS="${MAX_OUTPUT_ERRORS:-50}"

BASE_URL="http://${FB_HOST}:${FB_PORT}"

# ── Basic reachability + health endpoint ──────────────────────────────────────
health_json=$(curl -sf --max-time 3 "${BASE_URL}/api/v1/health") || {
    echo "UNHEALTHY: Fluent Bit HTTP server not reachable at ${BASE_URL}" >&2
    exit 1
}

status=$(echo "${health_json}" | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || true)
if [ "${status}" != "ok" ]; then
    echo "UNHEALTHY: health endpoint returned status='${status}'" >&2
    exit 1
fi

# ── Output error spike detection ──────────────────────────────────────────────
metrics_json=$(curl -sf --max-time 3 "${BASE_URL}/api/v1/metrics") || {
    echo "UNHEALTHY: cannot fetch metrics" >&2
    exit 1
}

# Sum proc_records.error across all output plugins
total_errors=$(echo "${metrics_json}" \
    | grep -o '"errors":[0-9]*' \
    | awk -F: '{sum += $2} END {print sum+0}')

if [ "${total_errors}" -gt "${MAX_OUTPUT_ERRORS}" ]; then
    echo "UNHEALTHY: cumulative output errors=${total_errors} exceeds threshold=${MAX_OUTPUT_ERRORS}" >&2
    exit 1
fi

echo "HEALTHY: status=${status}, output_errors=${total_errors}"
exit 0
