"""
Cost-tracker service.

Subscribes to Redis pub/sub for inference events, computes cost,
persists to Postgres with idempotent inserts, exposes per-tenant
usage via REST API.

Idempotency: events with duplicate request_ids are silently dropped.
Resilience: retries Postgres + Redis connections on startup with backoff.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from pydantic_settings import BaseSettings
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.responses import Response

from models import UsageRecord, TenantLimit, make_engine, make_session_factory, init_db
from pricing import calculate_cost, PRICING


# ──────────────────────────────────────────
# Settings
# ──────────────────────────────────────────
class Settings(BaseSettings):
    database_url: str = "postgresql://llm_user:llm_dev_password_change_me@postgres:5432/llmplatform"
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_channel: str = "inference_events"
    log_level: str = "INFO"

    # Connection retry config
    max_connect_attempts: int = 10
    max_backoff_seconds: int = 30

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
log = logging.getLogger("cost-tracker")


# ──────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────
EVENTS_PROCESSED = Counter(
    "cost_tracker_events_processed_total",
    "Total inference events processed",
    ["tenant", "backend", "status"],
)

EVENTS_DUPLICATE = Counter(
    "cost_tracker_events_duplicate_total",
    "Duplicate events silently ignored (idempotency wins)",
    ["tenant"],
)

EVENTS_INVALID = Counter(
    "cost_tracker_events_invalid_total",
    "Events rejected for missing/bad data",
    ["reason"],
)

COST_TOTAL = Counter(
    "cost_tracker_cost_usd_total",
    "Total cost in USD",
    ["tenant", "backend"],
)

TOKENS_TOTAL = Counter(
    "cost_tracker_tokens_total",
    "Total tokens processed",
    ["tenant", "type"],
)

DB_WRITE_ERRORS = Counter(
    "cost_tracker_db_errors_total",
    "Database write errors",
)


# ──────────────────────────────────────────
# Globals (set in lifespan)
# ──────────────────────────────────────────
engine = None
session_factory = None
redis_client = None
subscriber_task = None


# ──────────────────────────────────────────
# Connection retry helper
# ──────────────────────────────────────────
async def retry_with_backoff(name: str, fn):
    """Run an async fn with exponential backoff. Used for startup connections."""
    for attempt in range(1, settings.max_connect_attempts + 1):
        try:
            await fn()
            log.info(f"✅ {name} connected (attempt {attempt})")
            return
        except Exception as e:
            if attempt == settings.max_connect_attempts:
                log.error(f"❌ {name} connection failed after {attempt} attempts: {e}")
                raise
            wait = min(2 ** attempt, settings.max_backoff_seconds)
            log.warning(f"⏳ {name} connect attempt {attempt} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)


# ──────────────────────────────────────────
# Event processing (idempotent)
# ──────────────────────────────────────────
async def process_event(event: dict):
    """
    Compute cost and persist a usage record.

    INSERT ... ON CONFLICT DO NOTHING: if request_id exists,
    the duplicate is silently dropped at the DB layer.
    """
    request_id = event.get("request_id")
    if not request_id:
        EVENTS_INVALID.labels(reason="missing_request_id").inc()
        log.warning("Event missing request_id, dropping")
        return

    tenant   = event.get("tenant", "unknown")
    model    = event.get("model", "unknown")
    in_tok   = int(event.get("input_tokens", 0))
    out_tok  = int(event.get("output_tokens", 0))
    latency  = event.get("latency_ms")
    status   = event.get("status", "success")

    cost, backend = calculate_cost(model, in_tok, out_tok)

    async with session_factory() as session:
        try:
            stmt = pg_insert(UsageRecord).values(
                request_id=request_id,
                tenant_id=tenant,
                model=model,
                backend=backend,
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=in_tok + out_tok,
                cost_usd=cost,
                latency_ms=latency,
                status=status,
            ).on_conflict_do_nothing(index_elements=['request_id'])

            result = await session.execute(stmt)
            await session.commit()

            if result.rowcount == 0:
                EVENTS_DUPLICATE.labels(tenant=tenant).inc()
                log.debug(f"⚡ Duplicate event {request_id[:8]}, ignored")
                return

        except Exception as e:
            DB_WRITE_ERRORS.inc()
            log.exception(f"DB write failed for {request_id[:8]}: {e}")
            return

    EVENTS_PROCESSED.labels(tenant=tenant, backend=backend, status=status).inc()
    COST_TOTAL.labels(tenant=tenant, backend=backend).inc(cost)
    TOKENS_TOTAL.labels(tenant=tenant, type="input").inc(in_tok)
    TOKENS_TOTAL.labels(tenant=tenant, type="output").inc(out_tok)

    log.info(
        f"💰 Recorded: req={request_id[:8]} tenant={tenant} "
        f"model={model} tokens={in_tok}+{out_tok} cost=${cost:.6f}"
    )


# ──────────────────────────────────────────
# Redis subscriber
# ──────────────────────────────────────────
async def consume_events():
    global redis_client
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(settings.redis_channel)
    log.info(f"📡 Subscribed to: {settings.redis_channel}")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            event = json.loads(message["data"])
            await process_event(event)
        except json.JSONDecodeError:
            EVENTS_INVALID.labels(reason="bad_json").inc()
            log.warning(f"Invalid JSON: {message['data']!r}")
        except Exception as e:
            log.exception(f"Error processing event: {e}")


# ──────────────────────────────────────────
# FastAPI app + lifespan with retry
# ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, session_factory, redis_client, subscriber_task

    log.info("🚀 Cost-tracker starting...")
    log.info(f"  DB:    {settings.database_url.split('@')[-1]}")
    log.info(f"  Redis: {settings.redis_host}:{settings.redis_port}")

    # ── Connect to DB with retry
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    await retry_with_backoff("Postgres", lambda: init_db(engine))

    # ── Connect to Redis with retry
    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        decode_responses=True,
    )
    await retry_with_backoff("Redis", lambda: redis_client.ping())

    # ── Start subscriber
    subscriber_task = asyncio.create_task(consume_events())
    log.info("✅ Subscriber started")

    yield

    log.info("👋 Shutting down...")
    if subscriber_task:
        subscriber_task.cancel()
    if redis_client:
        await redis_client.close()
    if engine:
        await engine.dispose()


app = FastAPI(
    title="Cost Tracker",
    description="Idempotent per-tenant cost tracking for the LLM platform",
    version="0.3.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "cost-tracker",
        "version": "0.3.0",
        "features": [
            "idempotent_inserts",
            "redis_pubsub",
            "prometheus_metrics",
            "startup_retry",
        ],
        "endpoints": {
            "health":  "/healthz",
            "ready":   "/readyz",
            "metrics": "/metrics",
            "usage":   "/usage/{tenant_id}",
            "recent":  "/usage/{tenant_id}/recent",
            "limits":  "/limits/{tenant_id}",
            "pricing": "/pricing",
        },
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    try:
        await redis_client.ping()
        async with session_factory() as session:
            await session.execute(select(1))
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(503, f"Not ready: {e}")


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/usage/{tenant_id}")
async def get_usage(tenant_id: str):
    """Cumulative usage for a tenant.

    Known gap (v2): this scans usage_records directly.
    At scale, replace with usage_daily aggregate table.
    """
    async with session_factory() as session:
        stmt = (
            select(
                func.count(UsageRecord.id).label("requests"),
                func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageRecord.cost_usd), 0.0).label("total_cost"),
            )
            .where(UsageRecord.tenant_id == tenant_id)
        )
        result = await session.execute(stmt)
        row = result.one()

        return {
            "tenant_id":      tenant_id,
            "total_requests": row.requests,
            "input_tokens":   row.input_tokens,
            "output_tokens":  row.output_tokens,
            "total_tokens":   row.input_tokens + row.output_tokens,
            "total_cost_usd": round(row.total_cost, 6),
        }


@app.get("/usage/{tenant_id}/recent")
async def get_recent_usage(tenant_id: str, limit: int = 10):
    async with session_factory() as session:
        stmt = (
            select(UsageRecord)
            .where(UsageRecord.tenant_id == tenant_id)
            .order_by(UsageRecord.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        records = result.scalars().all()

        return {
            "tenant_id": tenant_id,
            "records": [
                {
                    "id":            r.id,
                    "request_id":    r.request_id,
                    "model":         r.model,
                    "backend":       r.backend,
                    "input_tokens":  r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cost_usd":      round(r.cost_usd, 6),
                    "latency_ms":    r.latency_ms,
                    "status":        r.status,
                    "created_at":    r.created_at.isoformat(),
                }
                for r in records
            ],
        }


@app.get("/limits/{tenant_id}")
async def get_tenant_limits(tenant_id: str):
    async with session_factory() as session:
        stmt = select(TenantLimit).where(TenantLimit.tenant_id == tenant_id)
        result = await session.execute(stmt)
        limit = result.scalar_one_or_none()

        if not limit:
            return {
                "tenant_id":          tenant_id,
                "daily_budget_usd":   10.0,
                "monthly_budget_usd": 100.0,
                "rate_limit_rpm":     60,
                "enabled":            True,
                "source":             "default",
            }

        return {
            "tenant_id":          limit.tenant_id,
            "daily_budget_usd":   limit.daily_budget_usd,
            "monthly_budget_usd": limit.monthly_budget_usd,
            "rate_limit_rpm":     limit.rate_limit_rpm,
            "enabled":            limit.enabled,
            "source":             "configured",
        }


@app.get("/pricing")
async def list_pricing():
    return {
        "models": [
            {
                "model":         name,
                "input_per_1m":  price.input_per_1m,
                "output_per_1m": price.output_per_1m,
                "backend":       price.backend,
            }
            for name, price in PRICING.items()
        ]
    }
