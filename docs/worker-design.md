# Worker Service Design

**Status:** Working prototype with idempotency + status tracking + concurrency cap

## What's implemented

- BLPOP-based queue consumer with N parallel worker tasks per pod
- **Idempotency lock** (Redis SETNX) — replayed jobs don't run twice
- **Status tracking** (pending → running → done/failed) in Redis HSET
- **Concurrency semaphore** caps in-flight router calls
- Calls router for inference, stores result in Redis (TTL 1h)
- Publishes inference events to cost-tracker channel
- Prometheus metrics: jobs, skipped, duration, queue depth, in-flight, errors
- Graceful shutdown via asyncio.Event

## Job lifecycle
