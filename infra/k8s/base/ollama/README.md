# Ollama Manifest (Stage 7 only)

This manifest is preserved for Stage 7 (GCP deployment) where Ollama 
(or vLLM) deploys on GPU node pools with proper PVC sizing.

**Stage 2 local-dev does NOT deploy this.** Reasons:
- Ollama image is ~4 GB, too heavy for local kind cluster with limited disk
- Heavy stateful inference services belong on GPU nodes (Stage 7)
- Stage 1 Docker Compose already validated end-to-end inference

For Stage 2 validation, the router uses a mock inference target.
