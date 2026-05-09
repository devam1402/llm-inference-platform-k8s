#!/usr/bin/env bash
# Tear down the kind cluster.

set -euo pipefail

YELLOW='\033[0;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

CLUSTER_NAME="llm-platform"

echo ""
echo -e "${YELLOW}${BOLD}⚠️  This will delete the kind cluster '${CLUSTER_NAME}'${NC}"
echo -e "  All pods, volumes, and state inside the cluster will be lost."
echo ""
read -p "Proceed? Type 'yes' to confirm: " confirm

if [ "$confirm" != "yes" ]; then
    echo -e "${RED}Aborted.${NC}"
    exit 1
fi

echo ""
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    kind delete cluster --name "${CLUSTER_NAME}"
    echo -e "${GREEN}${BOLD}✅ Cluster deleted${NC}"
else
    echo -e "${YELLOW}Cluster '${CLUSTER_NAME}' does not exist${NC}"
fi
