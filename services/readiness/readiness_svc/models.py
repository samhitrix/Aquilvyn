from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fm_common.db.base import Timestamps, UUIDPk, make_base

Base = make_base()
SCHEMA = "readiness"


class HoldingCheck(UUIDPk, Timestamps, Base):
    """The readiness engine's latest verdict for one holding of one person: each check's state and what it is
    doing about the ones that fail."""

    __tablename__ = "holding_checks"
    __table_args__ = (
        UniqueConstraint("household_id", "profile_id", "instrument_id", name="uq_holding_checks_holding"),
        Index("ix_readiness_holding_checks_household", "household_id"),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    rec_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(160))
    name: Mapped[str | None] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(24))
    profile_name: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16))  # ready | fixing | attention
    checks: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    fixes: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Sweep(UUIDPk, Base):
    """One pass of the engine over a household: why it ran, what it found, what it fixed."""

    __tablename__ = "sweeps"
    __table_args__ = (Index("ix_readiness_sweeps_household_started", "household_id", "started_at"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    trigger: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), server_default="running")  # running | done | failed
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
