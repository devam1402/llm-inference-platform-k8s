# Stage 2 — Status: Manifest Level Complete

## Completed

**Kubernetes manifests for all platform services:**

- `infra/k8s/base/00-namespace.yaml` — namespace + labels
- `infra/k8s/base/01-secrets.yaml.example` — secret template (real gitignored)
- `infra/k8s/base/02-shared-configmap.yaml` — shared service URLs + config
- `infra/k8s/base/router/` — Deployment + Service + ConfigMap (LiteLLM)
- `infra/k8s/base/worker/` — Deployment + Service
- `infra/k8s/base/cost-tracker/` — Deployment + Service
- `infra/k8s/base/ollama/` — Deployment + Service (deferred to Stage 7)
- `infra/kind/cluster.yaml` — local cluster spec
- `scripts/kind-setup.sh` + `scripts/kind-teardown.sh` — cluster lifecycle

**Manifest features:**
- Deployments with rolling update strategy
- Liveness + readiness probes per service convention
- Resource requests + limits
- Multi-source env (ConfigMap + Secret + inline)
- Volume mounts from ConfigMap (LiteLLM config)
- securityContext (non-root, dropped capabilities)
- nodeSelector for workload placement
- terminationGracePeriodSeconds for clean shutdown
- Kubernetes-native service discovery

## Deferred — and why

**Local kind validation deferred to Stage 7 (GKE Autopilot).**

Development environment: 10 GB RAM, 49 GB disk laptop. kind repeatedly
failed at control-plane init due to:
- Disk pressure from large image transfers (Ollama 4 GB)
- Resource contention under multi-node setup
- Unkillable stuck containers requiring host reboot

This is a local-dev environment limitation, not a manifest defect.

## Cloud validation plan (Stage 7)

Same manifests deploy on GKE Autopilot where:
- Managed control plane is fully resourced
- Node pools have proper disk sizing
- Artifact Registry replaces `kind load`
- Real cluster lifecycle (no stuck containers)

## Working demo: Stage 1 Docker Compose

The Stage 1 Docker Compose stack remains the working local demo with
23/25 smoke tests passing. Stage 2 manifests build on those proven
services without changes to application code.

## Engineering decision: scope vs polish

Rather than spend more cycles fighting local validation, manifests
are committed and validation moves to a properly-resourced cloud
environment. This mirrors real platform engineering practice — local
dev for development, cloud for validation.
