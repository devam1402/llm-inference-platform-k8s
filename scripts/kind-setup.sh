#!/usr/bin/env bash
# Create the local kind cluster for the LLM platform.
# Idempotent: skips if cluster already exists.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

CLUSTER_NAME="llm-platform"
CONFIG_FILE="$(dirname "$0")/../infra/kind/cluster.yaml"

# ── Check tools ──────────────────────────
echo -e "${BLUE}${BOLD}▶ Checking tools${NC}"
for tool in kind kubectl docker; do
    if ! command -v "$tool" &>/dev/null; then
        echo -e "  ${RED}❌ $tool not installed${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ $tool: $(which $tool)${NC}"
done

# ── Check Docker is running ─────────────
echo ""
echo -e "${BLUE}${BOLD}▶ Checking Docker${NC}"
if ! docker info &>/dev/null; then
    echo -e "  ${RED}❌ Docker daemon not running${NC}"
    exit 1
fi
mem=$(docker info 2>/dev/null | grep "Total Memory" | awk '{print $3}')
echo -e "  ${GREEN}✅ Docker running (memory: $mem)${NC}"

# ── Check if cluster exists ────────────
echo ""
echo -e "${BLUE}${BOLD}▶ Checking existing clusters${NC}"
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo -e "  ${YELLOW}⚠️  Cluster '${CLUSTER_NAME}' already exists${NC}"
    echo -e "  Skipping creation. To recreate: ${BOLD}./scripts/kind-teardown.sh${NC}"
else
    # ── Create cluster ─────────────────
    echo ""
    echo -e "${BLUE}${BOLD}▶ Creating cluster (takes 2-3 min)${NC}"
    kind create cluster --config "${CONFIG_FILE}" --wait 5m
fi

# ── Set kubectl context ────────────────
echo ""
echo -e "${BLUE}${BOLD}▶ Setting kubectl context${NC}"
kubectl config use-context "kind-${CLUSTER_NAME}"

# ── Verify nodes ───────────────────────
echo ""
echo -e "${BLUE}${BOLD}▶ Cluster nodes${NC}"
kubectl get nodes -o wide

echo ""
echo -e "${BLUE}${BOLD}▶ Node labels${NC}"
kubectl get nodes --show-labels | awk -F',' '{
    for (i=1; i<=NF; i++)
        if ($i ~ /workload=/) print "  " $i
}'

# ── Verify control plane is ready ─────
echo ""
echo -e "${BLUE}${BOLD}▶ Control-plane readiness${NC}"
kubectl wait --for=condition=Ready nodes --all --timeout=120s

# ── Done ──────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}🎉 Cluster ready${NC}"
echo ""
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Context: kind-${CLUSTER_NAME}"
echo "  Nodes:   $(kubectl get nodes --no-headers | wc -l)"
echo ""
echo "  Quick commands:"
echo "    kubectl get nodes"
echo "    kubectl get pods -A"
echo "    k9s    # interactive UI (if installed)"
echo ""
