from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fm_common.db.base import Timestamps, UUIDPk, make_base

Base = make_base()
SCHEMA = "advisor"


class AdvisorRun(UUIDPk, Base):
    __tablename__ = "runs"
    __table_args__ = {"schema": SCHEMA}

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    trigger: Mapped[str] = mapped_column(String(64))  # manual | nightly | event:<type>
    scope: Mapped[str] = mapped_column(String(64), server_default="household")  # household | profile:<uuid>
    rulebook_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    regime: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recommendation(UUIDPk, Timestamps, Base):
    """One verdict per (profile, instrument) or per portfolio-level finding. Versioned: when the
    action changes, the old row is ``superseded`` and the new one points to it."""

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_recs_household_status", "household_id", "status"),
        Index("ix_recs_subject", "household_id", "subject_key"),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.runs.id", ondelete="SET NULL"))
    subject_key: Mapped[str] = mapped_column(String(200))  # holding:<profile>:<instrument> | portfolio:<profile>:<rule>
    scope: Mapped[str] = mapped_column(String(20))           # holding | profile | household
    profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    asset_type: Mapped[str | None] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(24))          # ADD | ACCUMULATE | HOLD | TRIM | EXIT | SWITCH | REVIEW | REBALANCE
    horizon: Mapped[str] = mapped_column(String(12))         # long_term | short_term | n/a
    intent: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    priority: Mapped[float] = mapped_column(Float, server_default="0")
    bucket: Mapped[str] = mapped_column(String(12), server_default="fyi")  # urgent | focus | fyi
    actionable: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    rupee_impact: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    headline: Mapped[str] = mapped_column(Text)
    short_report: Mapped[dict] = mapped_column(JSONB)
    full_report: Mapped[dict] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))
    rulebook_version: Mapped[str] = mapped_column(String(20))
    rule_id: Mapped[str] = mapped_column(String(60))
    price_at_call: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    benchmark_at_call: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    ai_consensus: Mapped[str] = mapped_column(String(20), server_default="pending")  # pending | verified | caution | needs_review | rules_only | ai_failed
    narrative: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="open")  # open | accepted | snoozed | dismissed | superseded
    snooze_until: Mapped[date | None] = mapped_column(Date)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_note: Mapped[str | None] = mapped_column(Text)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    change_reason: Mapped[str | None] = mapped_column(Text)


class AIReview(UUIDPk, Base):
    __tablename__ = "ai_reviews"
    __table_args__ = (Index("ix_ai_reviews_rec", "recommendation_id"), {"schema": SCHEMA})

    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.recommendations.id", ondelete="CASCADE"))
    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(80))
    stance: Mapped[str] = mapped_column(String(12))  # agree | caution | disagree | error
    confidence: Mapped[float | None] = mapped_column(Float)
    issues: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    rationale: Mapped[str] = mapped_column(Text, server_default="")
    counter_case: Mapped[str] = mapped_column(Text, server_default="")
    suggested_action: Mapped[str | None] = mapped_column(String(16))  # what the AI itself would do
    plain_verdict: Mapped[str | None] = mapped_column(String(400))
    horizon: Mapped[str | None] = mapped_column(String(12))              # short_term | long_term (the AI's view)
    input_hash: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIProvider(UUIDPk, Timestamps, Base):
    __tablename__ = "ai_providers"
    __table_args__ = (UniqueConstraint("household_id", "provider", name="uq_ai_provider"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(20))       # claude | openai | gemini | groq | cloudflare | ollama
    model: Mapped[str] = mapped_column(String(80))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(12))
    base_url: Mapped[str | None] = mapped_column(String(255))
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    monthly_budget_usd: Mapped[float] = mapped_column(Float, server_default="10")
    priority: Mapped[int] = mapped_column(Integer, server_default="100")  # tried in this order; the next one is used if one fails


class ShadowTrade(UUIDPk, Base):
    """E37: the virtual trade an accepted recommendation implies. Never sent anywhere."""

    __tablename__ = "shadow_trades"
    __table_args__ = {"schema": SCHEMA}

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.recommendations.id", ondelete="CASCADE"))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(64))
    qty_delta: Mapped[Decimal] = mapped_column(Numeric(24, 8))  # +buy / -sell
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    executed_on: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecommendationOutcome(Base):
    """E38 scorecard: how each call played out after N days vs the benchmark."""

    __tablename__ = "recommendation_outcomes"
    __table_args__ = {"schema": SCHEMA}

    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.recommendations.id", ondelete="CASCADE"), primary_key=True)
    horizon_days: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    evaluated_on: Mapped[date] = mapped_column(Date)
    price_then: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    price_now: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    return_pct: Mapped[float] = mapped_column(Float)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(10))  # right | wrong | neutral
