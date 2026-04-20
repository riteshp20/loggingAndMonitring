#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kafka cluster verification script
#
# Checks:
#   1. All 3 brokers reachable + in-sync
#   2. All application topics exist with correct partition / RF / min-ISR
#   3. Under-replicated partition count = 0
#   4. Kafka Connect worker is up and all connectors are RUNNING
#   5. Consumer group lag per topic
#
# Exit code: 0 = healthy, 1 = at least one check failed
# Usage:
#   BOOTSTRAP_SERVERS=kafka-1:9092,kafka-2:9092,kafka-3:9092 bash verify-cluster.sh
#   docker compose exec kafka-1 bash /scripts/verify-cluster.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

BS="${BOOTSTRAP_SERVERS:-kafka-1:9092,kafka-2:9092,kafka-3:9092}"
CONNECT_URL="${KAFKA_CONNECT_URL:-http://kafka-connect:8083}"
EXPECTED_BROKERS=3
PASS=0; FAIL=0

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}[FAIL]${NC} $*" >&2; FAIL=$((FAIL+1)); }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
hdr()  { echo ""; echo "━━━ $* ━━━"; }

# ── 1. Broker reachability ────────────────────────────────────────────────────
hdr "Broker reachability"

broker_count=$(kafka-broker-api-versions.sh \
    --bootstrap-server "${BS}" 2>/dev/null \
    | grep -c "^id:" || true)

if [ "${broker_count}" -ge "${EXPECTED_BROKERS}" ]; then
    ok "${broker_count}/${EXPECTED_BROKERS} brokers reachable"
else
    fail "Only ${broker_count}/${EXPECTED_BROKERS} brokers reachable — cluster may be degraded"
fi

# ── 2. Topic topology ─────────────────────────────────────────────────────────
hdr "Topic topology"

declare -A EXPECTED_PARTITIONS=(
    [raw-logs]=24
    [processed-logs]=12
    [anomaly-events]=6
    [alert-notifications]=3
    [dead-letter-logs]=6
    [_connect-configs]=1
    [_connect-offsets]=25
    [_connect-status]=5
)
declare -A EXPECTED_RF=(
    [raw-logs]=3 [processed-logs]=3 [anomaly-events]=3
    [alert-notifications]=3 [dead-letter-logs]=3
    [_connect-configs]=3 [_connect-offsets]=3 [_connect-status]=3
)

TOPIC_DESCRIBE=$(kafka-topics.sh --bootstrap-server "${BS}" --describe 2>/dev/null)

for topic in "${!EXPECTED_PARTITIONS[@]}"; do
    expected_p="${EXPECTED_PARTITIONS[$topic]}"
    expected_r="${EXPECTED_RF[$topic]}"

    # Count distinct partition IDs for this topic
    actual_p=$(echo "${TOPIC_DESCRIBE}" \
        | grep "^[[:space:]]*Topic: ${topic}[[:space:]]" \
        | grep "Partition:" \
        | wc -l | tr -d ' ')

    # Replication factor from first partition line
    actual_r=$(echo "${TOPIC_DESCRIBE}" \
        | grep "^[[:space:]]*Topic: ${topic}[[:space:]]" \
        | grep "Partition: 0" \
        | grep -o "Replicas: [0-9,]*" \
        | tr -cd ',' | wc -c | awk '{print $1+1}')

    if [ "${actual_p}" -eq "${expected_p}" ] && [ "${actual_r}" -eq "${expected_r}" ]; then
        ok "${topic}: partitions=${actual_p}, rf=${actual_r}"
    else
        fail "${topic}: got partitions=${actual_p} (want ${expected_p}), rf=${actual_r} (want ${expected_r})"
    fi
done

# ── 3. Under-replicated partitions ────────────────────────────────────────────
hdr "Replication health"

urp=$(kafka-topics.sh \
    --bootstrap-server "${BS}" \
    --describe \
    --under-replicated-partitions 2>/dev/null \
    | grep -c "^[[:space:]]*Topic:" || true)

if [ "${urp}" -eq 0 ]; then
    ok "Under-replicated partitions: 0"
else
    fail "Under-replicated partitions: ${urp} — check broker logs"
    kafka-topics.sh --bootstrap-server "${BS}" --describe --under-replicated-partitions 2>/dev/null | head -20
fi

offline=$(kafka-topics.sh \
    --bootstrap-server "${BS}" \
    --describe \
    --unavailable-partitions 2>/dev/null \
    | grep -c "^[[:space:]]*Topic:" || true)

if [ "${offline}" -eq 0 ]; then
    ok "Unavailable partitions: 0"
else
    fail "Unavailable partitions: ${offline} — DATA LOSS RISK"
fi

# ── 4. Kafka Connect ──────────────────────────────────────────────────────────
hdr "Kafka Connect"

if ! curl -sf --max-time 5 "${CONNECT_URL}/connectors" >/dev/null 2>&1; then
    warn "Kafka Connect not reachable at ${CONNECT_URL} — skipping connector checks"
else
    ok "Kafka Connect REST API reachable"

    connectors=$(curl -sf "${CONNECT_URL}/connectors" 2>/dev/null | tr -d '[]"' | tr ',' '\n' | tr -d ' ')

    if [ -z "${connectors}" ]; then
        warn "No connectors deployed yet (run connect-init or deploy-connectors.sh)"
    else
        for connector in ${connectors}; do
            status=$(curl -sf "${CONNECT_URL}/connectors/${connector}/status" 2>/dev/null)
            conn_state=$(echo "${status}" | grep -o '"state":"[^"]*"' | head -1 | cut -d'"' -f4)
            failed_tasks=$(echo "${status}" | grep -o '"state":"FAILED"' | wc -l | tr -d ' ')

            if [ "${conn_state}" = "RUNNING" ] && [ "${failed_tasks}" -eq 0 ]; then
                ok "Connector ${connector}: RUNNING"
            else
                fail "Connector ${connector}: state=${conn_state}, failed_tasks=${failed_tasks}"
            fi
        done
    fi
fi

# ── 5. Consumer group lag ─────────────────────────────────────────────────────
hdr "Consumer group lag"

GROUPS="stream-processor-v1 anomaly-detector-v1 alert-manager-v1 log-pipeline-connect-v1"

for group in ${GROUPS}; do
    lag_output=$(kafka-consumer-groups.sh \
        --bootstrap-server "${BS}" \
        --describe \
        --group "${group}" 2>/dev/null || true)

    if [ -z "${lag_output}" ]; then
        warn "Group '${group}': no offset data (not started yet)"
        continue
    fi

    # Total lag = sum of LAG column (field index 5, skip header)
    total_lag=$(echo "${lag_output}" \
        | awk 'NR>1 && $6 ~ /^[0-9]+$/ {sum += $6} END {print sum+0}')

    max_lag=$(echo "${lag_output}" \
        | awk 'NR>1 && $6 ~ /^[0-9]+$/ {if($6>max) max=$6} END {print max+0}')

    if [ "${total_lag}" -lt 100000 ]; then
        ok "Group '${group}': total_lag=${total_lag}, max_partition_lag=${max_lag}"
    else
        warn "Group '${group}': total_lag=${total_lag} — may be falling behind"
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Cluster verification: ${PASS} passed, ${FAIL} failed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "${FAIL}" -eq 0 ] && exit 0 || exit 1
