from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Index, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fm_common.db.base import Timestamps, UUIDPk, make_base

Base = make_base()
SCHEMA = "market"


class AssetType(str, enum.Enum):
    STOCK = "stock"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    NPS = "nps"
    EPF = "epf"
    VPF = "vpf"
    PPF = "ppf"
    BOND = "bond"
    FIXED_DEPOSIT = "fixed_deposit"
    GOLD = "gold"          # SGB / digital / physical
    REIT = "reit"          # REITs & InvITs
    CRYPTO = "crypto"
    INDEX = "index"        # benchmarks, never held
    CASH = "cash"

    @property
    def is_accrual(self) -> bool:
        """Valued by contributions + declared interest rather than a market price."""
        return self in {AssetType.EPF, AssetType.VPF, AssetType.PPF, AssetType.FIXED_DEPOSIT, AssetType.CASH, AssetType.BOND}

    @property
    def is_exchange_traded(self) -> bool:
        return self in {AssetType.STOCK, AssetType.ETF, AssetType.REIT, AssetType.INDEX, AssetType.GOLD, AssetType.CRYPTO}


class Instrument(UUIDPk, Timestamps, Base):
    """Security master. ``household_id IS NULL`` → public instrument (RELIANCE.NS, an AMFI
    scheme); set → household-private instrument (an EPF account, an FD, a PPF account)."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("symbol", "asset_type", "household_id", name="uq_instruments_symbol_type_household", postgresql_nulls_not_distinct=True),
        Index("ix_instruments_symbol", "symbol"),
        Index("ix_instruments_isin", "isin"),
        Index("ix_instruments_name_lower", text("lower(name)")),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(64))          # RELIANCE.NS | 120503 (AMFI) | EPF-<id>
    name: Mapped[str] = mapped_column(String(255))
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType, name="asset_type", schema=SCHEMA, values_callable=lambda e: [m.value for m in e]))
    exchange: Mapped[str | None] = mapped_column(String(16))  # NSE | BSE | AMFI | PFRDA | EPFO
    currency: Mapped[str] = mapped_column(String(3), server_default="INR")
    isin: Mapped[str | None] = mapped_column(String(12))
    sector: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(120))
    benchmark_symbol: Mapped[str | None] = mapped_column(String(64))  # ^NSEI, ^CRSLDX (Nifty 500) …
    # Type-specific knobs, e.g. {"interest_rate": 8.25} (EPF), {"mf_category": "equity:flexi_cap",
    # "plan": "direct", "expense_ratio": 0.65, "amc": "PPFAS"} (MF), {"pfm": "SBI", "tier": "I", "scheme": "E"} (NPS)
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class PriceEOD(Base):
    __tablename__ = "prices_eod"
    __table_args__ = {"schema": SCHEMA}

    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.instruments.id", ondelete="CASCADE"), primary_key=True)
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    high: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    low: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), server_default="provider")


class Fundamentals(Base):
    __tablename__ = "fundamentals"
    __table_args__ = {"schema": SCHEMA}

    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.instruments.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CorporateActionType(str, enum.Enum):
    SPLIT = "split"
    BONUS = "bonus"
    DIVIDEND = "dividend"
    MERGER = "merger"      # P2
    DEMERGER = "demerger"  # P2


class CorporateAction(UUIDPk, Timestamps, Base):
    """E35. ``ratio_from:ratio_to`` — split 1:5 (1 old → 5 new), bonus 1:1 (1 free per 1 held)."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "action_type", "ex_date", name="uq_corp_action"),
        {"schema": SCHEMA},
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.instruments.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[CorporateActionType] = mapped_column(Enum(CorporateActionType, name="corporate_action_type", schema=SCHEMA, values_callable=lambda e: [m.value for m in e]))
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    ratio_from: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    ratio_to: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))  # dividend per share
    source: Mapped[str] = mapped_column(String(32), server_default="provider")
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
