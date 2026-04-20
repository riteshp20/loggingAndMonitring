#!/bin/bash
# First-time setup: validates dependencies, bootstraps .env, starts the stack,
# and waits for all services to be healthy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}==> $*${NC}"; }
error() { echo -e "${RED}ERROR: $*${NC}" >&2; }

# ── Dependency check ──────────────────────────────────────────────────────────
info "Checking prerequisites"

command -v docker >/dev/null 2>&1 \
    || { error "docker not found — install Docker Desktop or Docker Engine"; exit 1; }

# Support both 'docker-compose' (v1) and 'docker compose' (v2 plugin)
if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
elif docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    error "docker-compose / docker compose not found"
    exit 1
fi

# ── Environment file ──────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env created from .env.example — review before deploying to shared environments"
else
    info ".env already exists — skipping"
fi

# ── Build + start ─────────────────────────────────────────────────────────────
info "Building images"
${COMPOSE} build --pull --no-cache

info "Starting infrastructure (Kafka + Fluent Bit)"
${COMPOSE} up -d

# ── Wait for healthy ───────────────────────────────────────────────────────────
info "Waiting for all services to report healthy…"
MAX_WAIT=180
WAITED=0
INTERVAL=5

while true; do
    # Count services that are NOT yet healthy or running
    NOT_READY=$(${COMPOSE} ps --format json 2>/dev/null \
        | python3 -c "
import json, sys, re
data = sys.stdin.read().strip()
# compose ps --format json outputs one JSON object per line (not an array)
lines = [l for l in data.splitlines() if l.strip()]
not_ready = []
for line in lines:
    try:
        svc = json.loads(line)
    except json.JSONDecodeError:
        continue
    name   = svc.get('Name', svc.get('Service', '?'))
    status = svc.get('Health', svc.get('Status', ''))
    state  = svc.get('State', '')
    # kafka-init is expected to exit 0
    if name == 'kafka-init':
        if state not in ('exited', 'removing') and 'Exit 0' not in status:
            not_ready.append(name)
    elif status not in ('healthy', 'running') and state not in ('running',):
        not_ready.append(name)
for n in not_ready:
    print(n)
" 2>/dev/null || echo "parse_error")

    if [ -z "${NOT_READY}" ]; then
        info "All services are ready"
        break
    fi

    if [ "${WAITED}" -ge "${MAX_WAIT}" ]; then
        error "Services not healthy after ${MAX_WAIT}s: ${NOT_READY}"
        ${COMPOSE} logs --tail=40
        exit 1
    fi

    echo "  Waiting (${WAITED}s) — not yet ready: ${NOT_READY}"
    sleep "${INTERVAL}"
    WAITED=$((WAITED + INTERVAL))
done

# ── Smoke test ─────────────────────────────────────────────────────────────────
info "Running smoke test"
bash "${ROOT}/scripts/test_ingestion.sh"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
info "Pipeline is running"
echo ""
echo "  Kafka UI       : http://localhost:8080"
echo "  Fluent Bit API : http://localhost:2020/api/v1/metrics"
echo "  Fluent Bit Health: http://localhost:2020/api/v1/health"
echo ""
echo "  Send a test JSON log:"
echo "    printf '{\"level\":\"INFO\",\"message\":\"hello\",\"service_name\":\"test\"}\\n' | nc 127.0.0.1 5170"
echo ""
echo "  Tear down:"
echo "    docker compose down -v"
