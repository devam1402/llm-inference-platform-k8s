#!/usr/bin/env bash
# Pull required Ollama models into the running ollama container.
# Idempotent: skips models already present.
# Resilient: retries up to 5 times per model on failure.

set -euo pipefail

MODELS=("qwen2.5:0.5b")
MAX_ATTEMPTS=5
CONTAINER="ollama"

# Verify the ollama container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "❌ Container '${CONTAINER}' is not running."
    echo "   Run: docker compose up -d"
    exit 1
fi

# Wait for Ollama API to be reachable inside the container
echo "⏳ Waiting for Ollama API..."
for i in $(seq 1 30); do
    if docker exec "${CONTAINER}" ollama list >/dev/null 2>&1; then
        echo "✅ Ollama is responsive"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "❌ Ollama did not become ready in 60s"
        exit 1
    fi
    sleep 2
done

# Pull each model with retry
for model in "${MODELS[@]}"; do
    echo ""
    echo "📦 Checking model: ${model}"
    
    if docker exec "${CONTAINER}" ollama list 2>/dev/null | grep -q "^${model}"; then
        echo "✅ Already present: ${model}"
        continue
    fi
    
    echo "⬇️  Pulling ${model} (this takes 2-5 min)..."
    
    for attempt in $(seq 1 ${MAX_ATTEMPTS}); do
        if docker exec "${CONTAINER}" ollama pull "${model}"; then
            echo "✅ ${model} ready (attempt ${attempt})"
            break
        fi
        
        if [ "${attempt}" -eq "${MAX_ATTEMPTS}" ]; then
            echo "❌ Failed to pull ${model} after ${MAX_ATTEMPTS} attempts"
            exit 1
        fi
        
        wait_seconds=$((10 * attempt))
        echo "⚠️  Pull failed (attempt ${attempt}/${MAX_ATTEMPTS}). Retrying in ${wait_seconds}s..."
        sleep "${wait_seconds}"
    done
done

echo ""
echo "🎉 All models ready"
docker exec "${CONTAINER}" ollama list
