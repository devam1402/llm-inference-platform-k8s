"""
Worker service.

Background async job processor:
- Reads jobs from Redis queue (BLPOP inference_queue)
- Idempotency lock (SETNX) prevents replayed jobs from running twice
- Status tracking (pending/running/done/failed) in Redis HSET
- Concurrency cap via asyncio.Semaphore
- Calls router for inference, stores result in Redis (TTL 1h)
- Publishes inference event to cost-tracker channel

Exposes FastAPI for health, metrics, /enqueue, /result, /status.

See docs/worker-design.md for known v2 gaps.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from starlette.responses import Response


# ──────────────────────────────────────────
# Settings
# ──────────────────────────────────────────
class Settings(BaseSettings):
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""

    queue_name: str = "inference_queue"
    events_channel: str = "inference_events"
    result_ttl_seconds: int = 3600        # results expire after 1 hour
    lock_ttl_seconds: int = 60            # idempotency lock TTL (released on crash)

    router_url: str = "http://router:4000"
    router_master_key: str = "sk-test-master-key"

    worker_concurrency: int = 2           # parallel worker tasks per pod
    max_inflight: int = 4                 # semaphore cap on router calls
    request_timeout: int = 120

    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()


# ──────────────────────────────────────────
# Logging
# ──────────────────────────────────────────
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("worker")


# ──────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────
JOBS_CONSUMED = Counter(
    "worker_jobs_consumed_total",
    "Jobs pulled from queue and processed",
    ["status"],  # success | error | timeout
)

JOBS_SKIPPED = Counter(
    "worker_jobs_skipped_total",
    "Jobs skipped due to idempotency / lock",
    ["reason"],  # already_done | already_locked
)

JOB_DURATION = Histogram(
    "worker_job_duration_seconds",
    "End-to-end job processing time",
    ["status"],
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

QUEUE_DEPTH = Gauge(
    "worker_queue_depth",
    "Current depth of inference queue (sampled)",
)

INFLIGHT_REQUESTS = Gauge(
    "worker_inflight_requests",
    "Currently in-flight router calls",
)

ROUTER_ERRORS = Counter(
    "worker_router_errors_total",
    "Failed calls to router",
    ["reason"],
)


# ──────────────────────────────────────────
# Globals
# ──────────────────────────────────────────
redis_client: Optional[redis.Redis] = None
http_client: Optional[httpx.AsyncClient] = None
inflight_semaphore: Optional[asyncio.Semaphore] = None
worker_tasks: list = []
queue_depth_task: Optional[asyncio.Task] = None
shutdown_event = asyncio.Event()


# ──────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────
class JobRequest(BaseModel):
    """What the client submits to /enqueue."""
    tenant: str
    model: str = "tiny"
    messages: list[dict]
    max_tokens: int = 256
    temperature: float = 0.7


class JobEnvelope(BaseModel):
    """What gets stored on the queue."""
    job_id: str
    tenant: str
    model: str
    messages: list[dict]
    max_tokens: int
    temperature: float
    enqueued_at: float


# ──────────────────────────────────────────
# Status helpers
# ──────────────────────────────────────────
async def set_status(job_id: str, status: str, **extra):
    """Update job status in Redis HSET."""
    payload = {"status": status, "updated_at": str(time.time()), **{k: str(v) for k, v in extra.items()}}
    await redis_client.hset(f"status:{job_id}", mapping=payload)
    await redis_client.expire(f"status:{job_id}", settings.result_ttl_seconds)


# ──────────────────────────────────────────
# Job processing
# ──────────────────────────────────────────
async def process_job(envelope: dict) -> dict:
    """Call router for one job. Returns result dict. Publishes event to cost-tracker."""
    job_id = envelope["job_id"]
    tenant = envelope["tenant"]
    model  = envelope["model"]

    payload = {
        "model": model,
        "messages": envelope["messages"],
        "max_tokens": envelope["max_tokens"],
        "temperature": envelope["temperature"],
    }

    headers = {
        "Authorization": f"Bearer {settings.router_master_key}",
        "Content-Type": "application/json",
    }

    start = time.time()
    INFLIGHT_REQUESTS.inc()
    try:
        response = await http_client.post(
            f"{settings.router_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=settings.request_timeout,
        )
        response.raise_for_status()
        data = response.json()
        latency_ms = int((time.time() - start) * 1000)

        usage = data.get("usage", {})
        in_tok  = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)

        # Publish event for cost-tracker (idempotent on the consumer side via request_id)
        await redis_client.publish(
            settings.events_channel,
            json.dumps({
                "request_id":    job_id,
                "tenant":        tenant,
                "model":         data.get("model", model),
                "input_tokens":  in_tok,
                "output_tokens": out_tok,
                "latency_ms":    latency_ms,
                "status":        "success",
            }),
        )

        return {
            "status": "success",
            "job_id": job_id,
            "result": data,
            "latency_ms": latency_ms,
        }

    except httpx.TimeoutException:
        ROUTER_ERRORS.labels(reason="timeout").inc()
        log.error(f"Router timeout for job {job_id[:8]}")
        return {"status": "timeout", "job_id": job_id, "error": "Router timed out"}

    except httpx.HTTPStatusError as e:
        ROUTER_ERRORS.labels(reason=f"http_{e.response.status_code}").inc()
        log.error(f"Router HTTP {e.response.status_code} for job {job_id[:8]}")
        return {"status": "error", "job_id": job_id, "error": f"Router returned {e.response.status_code}"}

    except Exception as e:
        ROUTER_ERRORS.labels(reason="other").inc()
        log.exception(f"Unexpected error for job {job_id[:8]}")
        return {"status": "error", "job_id": job_id, "error": str(e)}

    finally:
        INFLIGHT_REQUESTS.dec()


# ──────────────────────────────────────────
# Worker loop with idempotency + status + semaphore
# ──────────────────────────────────────────
async def worker_loop(worker_id: int):
    """One worker pulls jobs in a loop until shutdown."""
    log.info(f"👷 Worker-{worker_id} started")

    while not shutdown_event.is_set():
        try:
            # Block up to 5s for a job
            popped = await redis_client.blpop(settings.queue_name, timeout=5)
            if popped is None:
                continue

            _key, raw = popped
            envelope = json.loads(raw)
            job_id = envelope.get("job_id", "?")

            # ───── Idempotency check 1: result already exists?
            already_done = await redis_client.exists(f"result:{job_id}")
            if already_done:
                JOBS_SKIPPED.labels(reason="already_done").inc()
                log.info(f"⚡ Job {job_id[:8]} already processed, skipping")
                continue

            # ───── Idempotency check 2: another worker holds the lock?
            lock_key = f"lock:{job_id}"
            got_lock = await redis_client.set(
                lock_key,
                str(worker_id),
                nx=True,
                ex=settings.lock_ttl_seconds,
            )
            if not got_lock:
                JOBS_SKIPPED.labels(reason="already_locked").inc()
                log.info(f"⚡ Job {job_id[:8]} already locked, skipping")
                continue

            log.info(f"👷 Worker-{worker_id} picked up job {job_id[:8]}")

            # Status: running
            await set_status(
                job_id,
                "running",
                worker_id=worker_id,
                started_at=time.time(),
            )

            start = time.time()

            # Concurrency cap across all workers in this pod
            async with inflight_semaphore:
                result = await process_job(envelope)

            duration = time.time() - start

            # Store result with TTL
            await redis_client.set(
                f"result:{job_id}",
                json.dumps(result),
                ex=settings.result_ttl_seconds,
            )

            # Status: done / failed
            await set_status(
                job_id,
                result["status"],
                completed_at=time.time(),
                duration_seconds=f"{duration:.2f}",
            )

            # Release the lock now that we're done
            await redis_client.delete(lock_key)

            JOBS_CONSUMED.labels(status=result["status"]).inc()
            JOB_DURATION.labels(status=result["status"]).observe(duration)

            log.info(
                f"👷 Worker-{worker_id} finished {job_id[:8]} "
                f"status={result['status']} duration={duration:.2f}s"
            )

        except json.JSONDecodeError:
            log.warning("Bad JSON on queue, dropping")
            continue
        except asyncio.CancelledError:
            log.info(f"👷 Worker-{worker_id} cancelled")
            break
        except Exception as e:
            log.exception(f"Worker-{worker_id} loop error: {e}")
            await asyncio.sleep(1)  # backoff on errors

    log.info(f"👷 Worker-{worker_id} stopped")


async def queue_depth_sampler():
    """Periodically sample queue depth for the gauge metric."""
    while not shutdown_event.is_set():
        try:
            depth = await redis_client.llen(settings.queue_name)
            QUEUE_DEPTH.set(depth)
        except Exception as e:
            log.warning(f"Queue depth sample failed: {e}")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            continue


# ──────────────────────────────────────────
# FastAPI lifespan
# ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, http_client, inflight_semaphore, worker_tasks, queue_depth_task

    log.info("🚀 Worker service starting...")
    log.info(f"  Redis:        {settings.redis_host}:{settings.redis_port}")
    log.info(f"  Queue:        {settings.queue_name}")
    log.info(f"  Router:       {settings.router_url}")
    log.info(f"  Concurrency:  {settings.worker_concurrency}")
    log.info(f"  Max inflight: {settings.max_inflight}")

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        decode_responses=True,
    )
    for attempt in range(1, 11):
        try:
            await redis_client.ping()
            log.info(f"✅ Redis connected (attempt {attempt})")
            break
        except Exception as e:
            if attempt == 10:
                log.error(f"❌ Redis connection failed after 10 attempts: {e}")
                raise
            wait = min(2 ** attempt, 30)
            log.warning(f"Redis connect attempt {attempt} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)

    http_client = httpx.AsyncClient(timeout=settings.request_timeout)
    log.info("✅ HTTP client ready")

    inflight_semaphore = asyncio.Semaphore(settings.max_inflight)
    log.info(f"✅ Semaphore set to {settings.max_inflight} max in-flight router calls")

    for i in range(settings.worker_concurrency):
        task = asyncio.create_task(worker_loop(i))
        worker_tasks.append(task)

    queue_depth_task = asyncio.create_task(queue_depth_sampler())
    log.info(f"✅ {settings.worker_concurrency} workers started")

    yield

    log.info("👋 Shutting down...")
    shutdown_event.set()

    if queue_depth_task:
        queue_depth_task.cancel()
    for task in worker_tasks:
        task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    if http_client:
        await http_client.aclose()
    if redis_client:
        await redis_client.close()


app = FastAPI(
    title="Worker",
    description="Async job processor for the LLM platform (with idempotency)",
    version="0.2.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "worker",
        "version": "0.2.0",
        "features": [
            "idempotency_lock",
            "status_tracking",
            "concurrency_semaphore",
            "prometheus_metrics",
        ],
        "endpoints": {
            "health":   "/healthz",
            "ready":    "/readyz",
            "metrics":  "/metrics",
            "enqueue":  "POST /enqueue",
            "result":   "GET /result/{job_id}",
            "status":   "GET /status/{job_id}",
            "depth":    "GET /queue/depth",
        },
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Verify Redis is reachable. Router is checked lazily on real requests."""
    try:
        await redis_client.ping()
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(503, f"Not ready: {e}")


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/enqueue")
async def enqueue_job(req: JobRequest):
    """Submit a job to the queue. Returns job_id for polling."""
    job_id = f"job_{uuid.uuid4().hex}"
    envelope = JobEnvelope(
        job_id=job_id,
        tenant=req.tenant,
        model=req.model,
        messages=req.messages,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        enqueued_at=time.time(),
    )

    await redis_client.rpush(settings.queue_name, envelope.model_dump_json())
    await set_status(job_id, "pending", tenant=req.tenant, model=req.model)

    log.info(f"📥 Enqueued {job_id[:8]} for tenant={req.tenant} model={req.model}")

    return {
        "job_id":     job_id,
        "status":     "queued",
        "result_url": f"/result/{job_id}",
        "status_url": f"/status/{job_id}",
    }


@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """Fetch result for a job. Returns 'pending_or_expired' if not ready."""
    raw = await redis_client.get(f"result:{job_id}")
    if raw is None:
        return {"job_id": job_id, "status": "pending_or_expired"}
    return json.loads(raw)


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Get current status of a job (pending/running/done/failed)."""
    status = await redis_client.hgetall(f"status:{job_id}")
    if not status:
        return {"job_id": job_id, "status": "unknown_or_expired"}
    return {"job_id": job_id, **status}


@app.get("/queue/depth")
async def queue_info():
    """Current queue depth (debug)."""
    depth = await redis_client.llen(settings.queue_name)
    return {"queue": settings.queue_name, "depth": depth}
