#!/bin/bash
# Integration smoke-test for the ingestion pipeline.
# Sends logs via all four input paths and verifies Fluent Bit is healthy.
# Exit code: 0 = all tests passed, 1 = at least one failure.
set -euo pipefail

FB_HOST="${FLUENT_BIT_HOST:-127.0.0.1}"
FB_TCP_PORT="${FLUENT_BIT_TCP_PORT:-5170}"
FB_UDP_PORT="${FLUENT_BIT_UDP_PORT:-5171}"
FB_API_PORT="${FLUENT_BIT_API_PORT:-2020}"

PASS=0; FAIL=0
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TRACE_ID=$(python3 -c "import uuid; print(uuid.uuid4().hex)" 2>/dev/null || echo "test-trace-001")

ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*" >&2; FAIL=$((FAIL+1)); }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Ingestion pipeline smoke test"
echo " Target: ${FB_HOST}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Test 1: Fluent Bit health endpoint ────────────────────────────────────────
echo ""
echo "Test 1 — Fluent Bit /api/v1/health"
if health=$(curl -sf --max-time 5 "http://${FB_HOST}:${FB_API_PORT}/api/v1/health"); then
    status=$(echo "${health}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
    if [ "${status}" = "ok" ]; then
        ok "Health endpoint returned status=ok"
    else
        fail "Health endpoint returned status=${status}"
    fi
else
    fail "Health endpoint not reachable at http://${FB_HOST}:${FB_API_PORT}/api/v1/health"
fi

# ── Test 2: Fluent Bit metrics endpoint ───────────────────────────────────────
echo ""
echo "Test 2 — Fluent Bit /api/v1/metrics"
if metrics=$(curl -sf --max-time 5 "http://${FB_HOST}:${FB_API_PORT}/api/v1/metrics"); then
    input_count=$(echo "${metrics}" | python3 -c "
import json, sys
m = json.load(sys.stdin)
total = sum(
    p.get('records', 0)
    for src in m.get('input', {}).values()
    for p in (src if isinstance(src, list) else [src])
    if isinstance(p, dict)
)
print(total)
" 2>/dev/null || echo "0")
    ok "Metrics reachable (total input records seen: ${input_count})"
else
    fail "Metrics endpoint not reachable"
fi

# ── Test 3: TCP JSON input ────────────────────────────────────────────────────
echo ""
echo "Test 3 — JSON record via TCP"
TCP_PAYLOAD="{\"timestamp\":\"${NOW}\",\"level\":\"INFO\",\"message\":\"smoke-test tcp\",\"service_name\":\"smoke-test\",\"trace_id\":\"${TRACE_ID}\"}"
if echo "${TCP_PAYLOAD}" | nc -q1 -w3 "${FB_HOST}" "${FB_TCP_PORT}" 2>/dev/null; then
    ok "Sent JSON record over TCP port ${FB_TCP_PORT}"
else
    fail "Failed to send over TCP port ${FB_TCP_PORT}"
fi

# ── Test 4: UDP JSON input ────────────────────────────────────────────────────
echo ""
echo "Test 4 — JSON record via UDP"
UDP_PAYLOAD="{\"timestamp\":\"${NOW}\",\"level\":\"INFO\",\"message\":\"smoke-test udp\",\"service_name\":\"smoke-test\",\"trace_id\":\"${TRACE_ID}\"}"
if echo "${UDP_PAYLOAD}" | nc -u -w1 "${FB_HOST}" "${FB_UDP_PORT}" 2>/dev/null; then
    ok "Sent JSON record over UDP port ${FB_UDP_PORT} (fire-and-forget; no ack)"
else
    fail "Failed to send over UDP port ${FB_UDP_PORT}"
fi

# ── Test 5: Syslog TCP input ──────────────────────────────────────────────────
echo ""
echo "Test 5 — RFC5424 syslog via TCP"
# PRI=134 = facility 16 (local0) + severity 6 (INFO)
SYSLOG_MSG="<134>1 ${NOW} smoke-host smoke-test - - - smoke test syslog message"
if echo "${SYSLOG_MSG}" | nc -q1 -w3 "${FB_HOST}" 5140 2>/dev/null; then
    ok "Sent RFC5424 syslog record on TCP port 5140"
else
    fail "Failed to send syslog on TCP port 5140"
fi

# ── Test 6: Verify Kafka connectivity (via Fluent Bit metrics delta) ──────────
echo ""
echo "Test 6 — Output delivery (10s observation window)"
metrics_before=$(curl -sf --max-time 5 "http://${FB_HOST}:${FB_API_PORT}/api/v1/metrics" 2>/dev/null || echo "{}")
sleep 10
metrics_after=$(curl -sf --max-time 5 "http://${FB_HOST}:${FB_API_PORT}/api/v1/metrics" 2>/dev/null || echo "{}")

output_delta=$(python3 - "${metrics_before}" "${metrics_after}" <<'EOF'
import json, sys
before = json.loads(sys.argv[1]) if sys.argv[1] != '{}' else {}
after  = json.loads(sys.argv[2]) if sys.argv[2] != '{}' else {}

def output_records(m):
    total = 0
    for src in m.get('output', {}).values():
        items = src if isinstance(src, list) else [src]
        for item in items:
            if isinstance(item, dict):
                total += item.get('proc_records', 0)
    return total

delta = output_records(after) - output_records(before)
print(delta)
EOF
)
if [ "${output_delta:-0}" -gt 0 ]; then
    ok "Kafka output delivered ${output_delta} records in observation window"
else
    fail "No records delivered to Kafka in 10s window — check 'docker compose logs fluent-bit'"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Results: ${PASS} passed, ${FAIL} failed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "${FAIL}" -gt 0 ]; then
    echo "Investigate with:"
    echo "  docker compose logs fluent-bit --tail=60"
    echo "  docker compose logs kafka --tail=30"
    exit 1
fi
exit 0
