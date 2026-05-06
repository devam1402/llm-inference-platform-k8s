"""
SQLAlchemy schema for cost tracking.

Design notes:
- request_id is UNIQUE → idempotent inserts (no double-billing)
- usage_records is append-only in spirit; we never UPDATE rows
- TenantLimit is the foundation for quota enforcement (Step 8 / v2)
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Index,
    UniqueConstraint, Boolean,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class UsageRecord(Base):
    """One row per inference request. Append-only. Idempotent via request_id."""
    
    __tablename__ = "usage_records"
    
    id              = Column(Integer, primary_key=True, autoincrement=True)
    request_id      = Column(String(64), unique=True, nullable=False, index=True)
    tenant_id       = Column(String(64), nullable=False, index=True)
    model           = Column(String(128), nullable=False)
    backend         = Column(String(32), nullable=False)
    input_tokens    = Column(Integer, nullable=False, default=0)
    output_tokens   = Column(Integer, nullable=False, default=0)
    total_tokens    = Column(Integer, nullable=False, default=0)
    cost_usd        = Column(Float, nullable=False, default=0.0)
    latency_ms      = Column(Integer, nullable=True)
    status          = Column(String(16), nullable=False, default="success")
    created_at      = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    
    __table_args__ = (
        Index("ix_tenant_created", "tenant_id", "created_at"),
        UniqueConstraint("request_id", name="uq_request_id"),
    )
    
    def __repr__(self):
        return (
            f"<UsageRecord req={self.request_id[:8]} tenant={self.tenant_id} "
            f"tokens={self.total_tokens} cost=${self.cost_usd:.6f}>"
        )


class TenantLimit(Base):
    """Per-tenant budget and rate limits. Used by router for quota enforcement."""
    
    __tablename__ = "tenant_limits"
    
    tenant_id           = Column(String(64), primary_key=True)
    daily_budget_usd    = Column(Float, nullable=False, default=10.0)
    monthly_budget_usd  = Column(Float, nullable=False, default=100.0)
    rate_limit_rpm      = Column(Integer, nullable=False, default=60)
    enabled             = Column(Boolean, nullable=False, default=True)
    created_at          = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at          = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return (
            f"<TenantLimit tenant={self.tenant_id} "
            f"daily=${self.daily_budget_usd} rpm={self.rate_limit_rpm}>"
        )


# ──────────────────────────────────────────
# Async engine + session factory
# ──────────────────────────────────────────
def make_engine(database_url: str):
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )


def make_session_factory(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
