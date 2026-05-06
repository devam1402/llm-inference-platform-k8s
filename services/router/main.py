"""Router service - LiteLLM proxy wrapped with FastAPI."""
import os
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("router")

REQUEST_COUNTER = Counter(
    "router_requests_total",
    "Total inference requests",
    ["model", "status"],
)

LATENCY_HISTOGRAM = Histogram(
    "router_request_duration_seconds",
    "Request latency in seconds",
    ["model"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

TOKEN_COUNTER = Counter(
    "router_tokens_total",
    "Total tokens processed",
    ["model", "type"],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Router service starting...")
    log.info(f"  Ollama URL: {os.getenv('OLLAMA_HOST', 'http://ollama:11434')}")
    yield
    log.info("Router service shutting down...")


app = FastAPI(
    title="LLM Router",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    if request.url.path == "/metrics":
        return await call_next(request)
    response = await call_next(request)
    duration = time.time() - start
    if "completions" in request.url.path:
        status = "success" if response.status_code < 400 else "error"
        REQUEST_COUNTER.labels(model="unknown", status=status).inc()
        LATENCY_HISTOGRAM.labels(model="unknown").observe(duration)
    return response


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def root():
    return {
        "service": "llm-router",
        "version": "0.1.0",
        "endpoints": {
            "health": "/healthz",
            "metrics": "/metrics",
            "openai_compat": "/v1/chat/completions",
        },
    }
