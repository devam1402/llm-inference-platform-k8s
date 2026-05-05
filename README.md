# 🚀 LLM Inference Platform on Kubernetes

> Production-grade, GPU-aware LLM serving platform built on Kubernetes with smart routing, autoscaling, and FinOps.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status: WIP](https://img.shields.io/badge/Status-WIP-orange.svg)]()

## 🚧 Status

This project is under active development. See [docs/roadmap.md](docs/roadmap.md) for progress.

## 📋 Quick links

- [Architecture overview](docs/architecture.md) (coming soon)
- [Local setup guide](docs/local-setup.md) (coming soon)
- [Cost model](docs/cost-model.md) (coming soon)

## 🛠️ Tech Stack

- **Inference:** vLLM (cloud), Ollama (local)
- **Routing:** LiteLLM
- **Orchestration:** Kubernetes (kind locally, GKE on GCP)
- **Autoscaling:** KEDA + Karpenter
- **Observability:** Prometheus + Grafana + Jaeger
- **FinOps:** OpenCost + custom cost-tracker

## 🚀 Quick Start (coming soon)

```bash
# Local development
make local

# Run smoke test
make smoke
```

## 📄 License

MIT — see [LICENSE](LICENSE)
