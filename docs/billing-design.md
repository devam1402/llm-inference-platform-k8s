# Billing System Design

**Status:** Working prototype (Step 6 complete)

## What's implemented

- Per-tenant cost tracking persisted in Postgres
- Pricing for self-hosted (Ollama, vLLM), OpenAI, Anthropic models
- Redis pub/sub for async event ingestion
- Idempotent inserts via request_id UNIQUE constraint + ON CONFLICT DO NOTHING
- Prometheus metrics: events processed, duplicates ignored, cost, tokens
- REST API: /usage/{tenant}, /usage/{tenant}/recent, /limits/{tenant}, /pricing
- TenantLimit table (foundation for quota enforcement in Step 8)

## Why idempotency matters

Distributed messaging systems (Redis Pub/Sub, Kafka, SQS) guarantee at-least-once delivery.
Duplicate events are normal during:

- Network retries between router and Redis
- Consumer crashes mid-processing
- Replay after incidents

Without idempotency, duplicates double-charge customers. With ON CONFLICT DO NOTHING
on request_id, the database silently drops duplicates - the system is structurally
incapable of double-billing.

## Event format (router to cost-tracker)

If request_id is missing, the event is dropped and a metric is incremented.

## Schema

usage_records:
- id              PK, auto-increment
- request_id      UNIQUE, indexed (idempotency key)
- tenant_id       indexed
- model
- backend         self-hosted | openai | anthropic
- input_tokens
- output_tokens
- total_tokens
- cost_usd
- latency_ms
- status
- created_at      indexed

tenant_limits:
- tenant_id           PK
- daily_budget_usd
- monthly_budget_usd
- rate_limit_rpm
- enabled
- created_at
- updated_at

## Production gaps (deferred to v2)

This is a working prototype, not production-grade. The following gaps are
intentionally deferred:

1. No aggregation layer (hourly/daily) - slow dashboards at scale
2. Pricing in code, not DB - redeploy needed to change prices
3. No quota enforcement in router - tenants can exceed budget
4. Redis Pub/Sub loses on disconnect - lost billing events
5. No append-only constraint - audit/compliance risk
6. No reconciliation jobs - hard to detect drift/bugs
7. No SLOs or alerting rules - issues go unnoticed
8. Per-tenant API keys not enforced - single master key only

## When this is production-grade

This service can honestly be called production-grade once all 8 gaps are
closed and verified:

- [ ] End-to-end idempotency verified under retry chaos tests
- [ ] Hourly/daily aggregates power dashboards
- [ ] Pricing versioned and stored per event
- [ ] Router enforces quotas using fast cache
- [ ] Durable event pipeline (no data loss)
- [ ] SLOs + alerts in place
- [ ] Audit/reconciliation jobs pass consistently
- [ ] Security boundaries (tenant + secrets) enforced

Until then, accurate label is "working prototype".
