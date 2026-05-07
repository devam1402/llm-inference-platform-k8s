#!/usr/bin/env bash
# Smoke test for the LLM Inference Platform.
# Verifies all services + end-to-end flow.
# Exit 0 if all checks pass, 1 if any fail.

set -uo pipefail

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────
ROUTER_URL="http://localhost:4000"
WORKER_URL="http://localhost:8002"
COST_URL="http://localhost:8001"
PROMETHEUS_URL="http://localhost:9090"
GRAFANA_URL="http://localhost:3000"

ROUTER_KEY="${ROUTER_MASTER_KEY:-sk-dev-router-key-change-me}"
TENANT="smoke-$(date +%s)"

# ──────────────────────────────────────────
# Colors
# ──────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────
say() { echo -e "${BLUE}${BOLD}▶ $1${NC}"; }

pass() {
    echo -e "  ${GREEN}✅ $1${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
    echo -e "  ${RED}❌ $1${NC}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

skip() {
    echo -e "  ${YELLOW}⏭  $1${NC}"
    SKIP_COUNT=$((SKIP_COUNT + 1))
}

http_status() {
    curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$@"
}

# ──────────────────────────────────────────
# Banner
# ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🚀 LLM Platform Smoke Test"
echo "═══════════════════════════════════════════════════════════"
echo "  Tenant: $TENANT"
echo "  Date:   $(date)"
echo ""

# ══════════════════════════════════════════
# Section 1: Service health
# ══════════════════════════════════════════
say "Section 1: Service health checks"

services=(
    "ollama:11434:/"
    "router:4000:/health/liveliness"
    "worker:8002:/healthz"
    "cost-tracker:8001:/healthz"
    "prometheus:9090:/-/healthy"
    "grafana:3000:/api/health"
)

for entry in "${services[@]}"; do
    IFS=':' read -r name port path <<< "$entry"
    code=$(http_status "http://localhost:${port}${path}")
    if [[ "$code" =~ ^(200|301|302)$ ]]; then
        pass "$name reachable (HTTP $code)"
    else
        fail "$name unreachable (HTTP $code)"
    fi
done

echo ""

# ══════════════════════════════════════════
# Section 2: Sync path (router → ollama)
# ══════════════════════════════════════════
say "Section 2: Sync path — router → ollama"

# 2a: Happy path
response=$(curl -s -X POST "$ROUTER_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ROUTER_KEY" \
    -d "{
        \"model\": \"tiny\",
        \"messages\": [{\"role\": \"user\", \"content\": \"Reply with hi\"}],
        \"max_tokens\": 10
    }" 2>&1)

if echo "$response" | grep -q '"content"'; then
    pass "Sync request returned valid response"
else
    fail "Sync request failed: $response"
fi

# 2b: Bad auth → 401
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$ROUTER_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer wrong-key" \
    -d '{"model":"tiny","messages":[{"role":"user","content":"hi"}]}')

if [ "$code" = "401" ]; then
    pass "Bad API key correctly rejected (HTTP 401)"
else
    fail "Expected 401 for bad auth, got HTTP $code"
fi

# 2c: Models endpoint
code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $ROUTER_KEY" \
    "$ROUTER_URL/v1/models")

if [ "$code" = "200" ]; then
    pass "GET /v1/models returns 200"
else
    fail "GET /v1/models returned HTTP $code"
fi

echo ""

# ══════════════════════════════════════════
# Section 3: Async path (worker queue)
# ══════════════════════════════════════════
say "Section 3: Async path — worker queue"

# 3a: Enqueue
job_response=$(curl -s -X POST "$WORKER_URL/enqueue" \
    -H "Content-Type: application/json" \
    -d "{
        \"tenant\": \"$TENANT\",
        \"model\": \"tiny\",
        \"messages\": [{\"role\": \"user\", \"content\": \"Say hi\"}],
        \"max_tokens\": 10
    }")

job_id=$(echo "$job_response" | python3 -c "import sys, json; print(json.load(sys.stdin).get('job_id', ''))" 2>/dev/null)

if [ -n "$job_id" ]; then
    pass "Enqueue returned job_id: ${job_id:0:16}..."
else
    fail "Enqueue did not return a job_id: $job_response"
    job_id=""
fi

# 3b: Poll status until done (max 60s)
if [ -n "$job_id" ]; then
    final_status=""
    for i in $(seq 1 30); do
        status=$(curl -s "$WORKER_URL/status/$job_id" \
            | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', ''))" 2>/dev/null)
        if [ "$status" = "success" ] || [ "$status" = "error" ] || [ "$status" = "timeout" ] || [ "$status" = "failed" ]; then
            final_status="$status"
            break
        fi
        sleep 2
    done
    
    if [ "$final_status" = "success" ]; then
        pass "Job completed with status=success"
    else
        fail "Job did not complete cleanly (final status: $final_status)"
    fi
fi

# 3c: Fetch result
if [ -n "$job_id" ]; then
    result=$(curl -s "$WORKER_URL/result/$job_id")
    if echo "$result" | grep -q '"status".*"success"'; then
        pass "Result fetch returns success payload"
    else
        fail "Result missing or not success: $result"
    fi
fi

# 3d: Queue depth
depth_response=$(curl -s "$WORKER_URL/queue/depth")
depth=$(echo "$depth_response" | python3 -c "import sys, json; print(json.load(sys.stdin).get('depth', -1))" 2>/dev/null)

if [ "$depth" -ge 0 ] 2>/dev/null; then
    pass "Queue depth endpoint works (current: $depth)"
else
    fail "Queue depth endpoint broken: $depth_response"
fi

echo ""

# ══════════════════════════════════════════
# Section 4: Cost tracking
# ══════════════════════════════════════════
say "Section 4: Cost tracking — event flow"

# Wait for cost-tracker to consume the event
sleep 4

# 4a: Check usage endpoint
usage=$(curl -s "$COST_URL/usage/$TENANT")
total_requests=$(echo "$usage" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total_requests', 0))" 2>/dev/null)

if [ "$total_requests" -ge 1 ] 2>/dev/null; then
    pass "Cost-tracker recorded $total_requests request(s) for tenant"
else
    fail "Cost-tracker did not record any requests: $usage"
fi

# 4b: Check Postgres directly
pg_count=$(docker exec postgres psql -U llm_user -d llmplatform -t -c \
    "SELECT COUNT(*) FROM usage_records WHERE tenant_id = '$TENANT';" 2>/dev/null | tr -d ' ')

if [ "$pg_count" -ge 1 ] 2>/dev/null; then
    pass "Postgres has $pg_count row(s) for tenant"
else
    fail "Postgres has no rows for tenant"
fi

# 4c: Recent endpoint
recent=$(curl -s "$COST_URL/usage/$TENANT/recent?limit=5")
record_count=$(echo "$recent" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('records', [])))" 2>/dev/null)

if [ "$record_count" -ge 1 ] 2>/dev/null; then
    pass "Recent endpoint returns $record_count record(s)"
else
    fail "Recent endpoint returns no records"
fi

# 4d: Pricing endpoint
code=$(http_status "$COST_URL/pricing")
if [ "$code" = "200" ]; then
    pass "Pricing endpoint returns 200"
else
    fail "Pricing endpoint returned HTTP $code"
fi

echo ""

# ══════════════════════════════════════════
# Section 5: Idempotency
# ══════════════════════════════════════════
say "Section 5: Idempotency — duplicate event rejection"

# Get current duplicate counter from Prometheus
dup_before=$(curl -s "$PROMETHEUS_URL/api/v1/query?query=cost_tracker_events_duplicate_total" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    total = sum(float(r['value'][1]) for r in d['data']['result'])
    print(int(total))
except Exception:
    print(0)
" 2>/dev/null)

# Publish a duplicate event manually via Redis
fixed_id="duplicate-test-$(date +%s)"
duplicate_event="{\"request_id\":\"$fixed_id\",\"tenant\":\"$TENANT\",\"model\":\"ollama/qwen2.5:0.5b\",\"input_tokens\":10,\"output_tokens\":10,\"latency_ms\":100,\"status\":\"success\"}"

# Send same event twice
docker exec redis redis-cli PUBLISH inference_events "$duplicate_event" > /dev/null
docker exec redis redis-cli PUBLISH inference_events "$duplicate_event" > /dev/null

sleep 4

# Count rows for this request_id (should be 1, not 2)
dup_rows=$(docker exec postgres psql -U llm_user -d llmplatform -t -c \
    "SELECT COUNT(*) FROM usage_records WHERE request_id = '$fixed_id';" 2>/dev/null | tr -d ' ')

if [ "$dup_rows" = "1" ]; then
    pass "Duplicate event correctly stored only once (1 row, not 2)"
else
    fail "Duplicate event handling broken: $dup_rows rows found"
fi

# Verify duplicate counter incremented
dup_after=$(curl -s "$PROMETHEUS_URL/api/v1/query?query=cost_tracker_events_duplicate_total" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    total = sum(float(r['value'][1]) for r in d['data']['result'])
    print(int(total))
except Exception:
    print(0)
" 2>/dev/null)

if [ "$dup_after" -gt "$dup_before" ] 2>/dev/null; then
    pass "Duplicate counter incremented (before=$dup_before, after=$dup_after)"
else
    skip "Duplicate counter unchanged (Prometheus may need 15s to scrape)"
fi

echo ""

# ══════════════════════════════════════════
# Section 6: Observability
# ══════════════════════════════════════════
say "Section 6: Observability — Prometheus + Grafana"

# 6a: All Prometheus targets up
targets_up=$(curl -s "$PROMETHEUS_URL/api/v1/targets" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    active = d.get('data', {}).get('activeTargets', [])
    up = sum(1 for t in active if t.get('health') == 'up')
    print(up)
except Exception:
    print(0)
" 2>/dev/null)

if [ "$targets_up" -ge 4 ] 2>/dev/null; then
    pass "Prometheus has $targets_up healthy scrape targets"
else
    fail "Only $targets_up healthy targets (expected ≥ 4)"
fi

# 6b: Specific metrics exist
metrics=("router_requests_total" "worker_jobs_consumed_total" "cost_tracker_events_processed_total")
for metric in "${metrics[@]}"; do
    has_data=$(curl -s "$PROMETHEUS_URL/api/v1/query?query=$metric" \
        | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('yes' if d.get('data', {}).get('result') else 'no')
except Exception:
    print('no')
" 2>/dev/null)
    
    if [ "$has_data" = "yes" ]; then
        pass "Metric exists: $metric"
    else
        fail "Metric missing: $metric"
    fi
done

# 6c: Grafana datasource works
ds_check=$(curl -s -u admin:admin "$GRAFANA_URL/api/datasources/name/Prometheus" 2>/dev/null)
if echo "$ds_check" | grep -q '"type":"prometheus"'; then
    pass "Grafana Prometheus datasource configured"
else
    fail "Grafana Prometheus datasource not found"
fi

# 6d: Grafana dashboard exists
dash_check=$(curl -s -u admin:admin "$GRAFANA_URL/api/dashboards/uid/llm-overview" 2>/dev/null)
if echo "$dash_check" | grep -q '"uid":"llm-overview"'; then
    pass "Grafana dashboard llm-overview loaded"
else
    fail "Grafana dashboard llm-overview not found"
fi

echo ""

# ══════════════════════════════════════════
# Summary
# ══════════════════════════════════════════
TOTAL=$((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))

echo "═══════════════════════════════════════════════════════════"
echo "  📊 Results"
echo "═══════════════════════════════════════════════════════════"
echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT / $TOTAL"
echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT / $TOTAL"
echo -e "  ${YELLOW}Skipped:${NC} $SKIP_COUNT / $TOTAL"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}🎉 ALL CHECKS PASSED — platform is healthy!${NC}"
    echo ""
    echo "  Service URLs:"
    echo "    • Router:     $ROUTER_URL"
    echo "    • Worker:     $WORKER_URL"
    echo "    • Cost:       $COST_URL"
    echo "    • Prometheus: $PROMETHEUS_URL"
    echo "    • Grafana:    $GRAFANA_URL  (admin/admin)"
    echo "    • Jaeger:     http://localhost:16686"
    echo ""
    exit 0
else
    echo -e "  ${RED}${BOLD}❌ Some checks failed. Investigate before relying on the stack.${NC}"
    echo ""
    echo "  Debug commands:"
    echo "    docker compose ps"
    echo "    docker compose logs <service> --tail=30"
    echo ""
    exit 1
fi
