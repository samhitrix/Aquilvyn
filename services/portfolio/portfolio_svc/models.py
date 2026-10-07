from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fm_common.db.base import SyncTracked, Timestamps, UUIDPk, make_base

Base = make_base()
SCHEMA = "core"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, schema=SCHEMA, values_callable=lambda x: [m.value for m in x])


class Relationship(str, enum.Enum):
    SELF = "self"
    SPOUSE = "spouse"
    PARENT = "parent"
    CHILD = "child"
    SIBLING = "sibling"
    HUF = "huf"
    OTHER = "other"


class RiskProfile(str, enum.Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class Profile(UUIDPk, SyncTracked, Base):
    """An investing entity inside a household (self, spouse, parent, child, HUF)."""

    __tablename__ = "profiles"
    __table_args__ = (
        Index("ix_profiles_household", "household_id"),
        # one live profile per linked member — makes the first-login bootstrap race-safe
        Index("uq_profiles_household_linked_user", "household_id", "linked_user_id", unique=True,
              postgresql_where=text("linked_user_id IS NOT NULL AND deleted_at IS NULL")),
        # one PAN per household — the PAN in a statement decides which profile it belongs to
        Index("uq_profiles_household_pan_hash", "household_id", "pan_hash", unique=True,
              postgresql_where=text("pan_hash IS NOT NULL AND deleted_at IS NULL")),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    display_name: Mapped[str] = mapped_column(String(120))
    relationship: Mapped[Relationship] = mapped_column(_enum(Relationship, "relationship"), server_default="self")
    linked_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # member who *is* this profile
    pan_encrypted: Mapped[str | None] = mapped_column(Text)  # AES-GCM, see fm_common.crypto
    pan_last4: Mapped[str | None] = mapped_column(String(4))
    pan_hash: Mapped[str | None] = mapped_column(String(64))  # fm_common.crypto.pan_fingerprint — searchable, not reversible
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    email: Mapped[str | None] = mapped_column(String(254))   # optional contact details
    mobile: Mapped[str | None] = mapped_column(String(20))
    tax_slab_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_profile: Mapped[RiskProfile] = mapped_column(_enum(RiskProfile, "risk_profile"), server_default="moderate")
    retirement_age: Mapped[int] = mapped_column(Integer, server_default="60")
    # {"equity": 60, "debt": 30, "gold": 10}
    target_allocation: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Drawdown Sentinel thresholds per intent, % — {"trade": 8, "satellite": 15, "core": 25}
    drawdown_thresholds: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    color: Mapped[str | None] = mapped_column(String(9))


class AccessLevel(str, enum.Enum):
    READ = "read"
    WRITE = "write"


class ProfileAccess(Base):
    """Which household members may see/manage which profiles (owner/admin see all)."""

    __tablename__ = "profile_access"
    __table_args__ = {"schema": SCHEMA}

    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    level: Mapped[AccessLevel] = mapped_column(_enum(AccessLevel, "access_level"), server_default="write")


class ProfileGroup(UUIDPk, SyncTracked, Base):
    """Custom consolidated views, e.g. 'Parents', 'Kids education'."""

    __tablename__ = "profile_groups"
    __table_args__ = {"schema": SCHEMA}

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(120))
    profile_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), server_default=text("'{}'"))


class PortfolioKind(str, enum.Enum):
    BROKER = "broker"          # a demat / trading account
    MUTUAL_FUNDS = "mutual_funds"
    RETIREMENT = "retirement"  # EPF / PPF / NPS
    DEPOSITS = "deposits"      # FDs, bonds
    OTHER = "other"


class Portfolio(UUIDPk, SyncTracked, Base):
    __tablename__ = "portfolios"
    __table_args__ = (Index("ix_portfolios_household_profile", "household_id", "profile_id"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[PortfolioKind] = mapped_column(_enum(PortfolioKind, "portfolio_kind"), server_default="broker")
    broker: Mapped[str | None] = mapped_column(String(60))  # zerodha | groww | upstox | cams | …
    base_currency: Mapped[str] = mapped_column(String(3), server_default="INR")


class TxnType(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    SIP = "sip"
    DIVIDEND = "dividend"
    INTEREST = "interest"          # EPF/PPF/FD interest credit
    CONTRIBUTION = "contribution"  # EPF/NPS/PPF deposit
    WITHDRAWAL = "withdrawal"
    SPLIT = "split"                # quantity = new/old ratio (e.g. 5 for 1:5)
    BONUS = "bonus"                # quantity = bonus units received
    SWITCH_IN = "switch_in"
    SWITCH_OUT = "switch_out"
    FEE = "fee"

    @property
    def adds_units(self) -> bool:
        return self in {TxnType.BUY, TxnType.SIP, TxnType.CONTRIBUTION, TxnType.INTEREST, TxnType.BONUS, TxnType.SWITCH_IN}

    @property
    def removes_units(self) -> bool:
        return self in {TxnType.SELL, TxnType.WITHDRAWAL, TxnType.SWITCH_OUT}


class Transaction(UUIDPk, SyncTracked, Base):
    """The ledger — single source of truth. Instrument identity is denormalised (symbol,
    asset_type) so holdings can be computed without a round-trip to market-svc."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_portfolio_date", "portfolio_id", "trade_date"),
        Index("ix_transactions_household_updated", "household_id", "updated_at"),
        Index("ix_transactions_instrument", "instrument_id"),
        UniqueConstraint("portfolio_id", "client_ref", name="uq_transactions_client_ref"),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.portfolios.id", ondelete="CASCADE"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_type: Mapped[str] = mapped_column(String(24))
    txn_type: Mapped[TxnType] = mapped_column(_enum(TxnType, "txn_type"))
    trade_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), server_default="0")
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), server_default="0")
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 4), server_default="0")
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), server_default="0")  # gross cash value (always ≥0)
    notes: Mapped[str] = mapped_column(Text, server_default="")
    source: Mapped[str] = mapped_column(String(64), server_default="manual")  # manual | import:<job-uuid> | corporate_action
    client_ref: Mapped[str | None] = mapped_column(String(80))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class HoldingIntent(str, enum.Enum):
    CORE = "core"              # long-term compounder
    SATELLITE = "satellite"    # medium-term / thematic
    TRADE = "trade"            # short-term, stop + target
    RETIREMENT = "retirement"
    GOAL = "goal"


class HoldingPref(UUIDPk, SyncTracked, Base):
    """E18 Holding Intent + thesis tracker + per-holding overrides."""

    __tablename__ = "holding_prefs"
    __table_args__ = (UniqueConstraint("profile_id", "instrument_id", name="uq_holding_prefs"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # Intent is a property of the *person's* holding (tax & horizon are per PAN), not the account.
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    intent: Mapped[HoldingIntent | None] = mapped_column(_enum(HoldingIntent, "holding_intent"))
    intent_source: Mapped[str] = mapped_column(String(10), server_default="auto")  # auto | user
    goal_name: Mapped[str | None] = mapped_column(String(120))
    thesis: Mapped[str] = mapped_column(Text, server_default="")
    # [{"metric": "roe", "op": ">=", "value": 18}, …] — re-checked by the Advisor
    thesis_checks: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    drawdown_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    # the person's own answer to "is a SIP running in this fund?" — None: guess from purchase dates (fm_common.sip)
    sip: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class ProfileAccount(UUIDPk, Timestamps, Base):
    """A broker / RTA account identity (e.g. Zerodha client ID) linked to a profile, so the next
    statement from that account is routed to the right family member automatically."""

    __tablename__ = "profile_accounts"
    __table_args__ = (UniqueConstraint("household_id", "kind", "fingerprint", name="uq_profile_accounts_fp"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))          # zerodha | …
    fingerprint: Mapped[str] = mapped_column(String(64))   # fm_common.crypto.account_fingerprint
    label: Mapped[str] = mapped_column(String(40))         # masked, for display (e.g. "Zerodha ••0001")


class ImportTemplate(UUIDPk, Timestamps, Base):
    """A column mapping the household confirmed once for a broker file layout Aquilvyn didn't know.
    Keyed by a fingerprint of the header row (column names only — no values are ever stored)."""

    __tablename__ = "import_templates"
    __table_args__ = (UniqueConstraint("household_id", "fingerprint", name="uq_import_templates_layout"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    fingerprint: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))          # trades | holdings
    mapping: Mapped[dict] = mapped_column(JSONB)           # {field: header text}
    label: Mapped[str] = mapped_column(String(120))        # e.g. "Upstox trades" / the file name
    uses: Mapped[int] = mapped_column(Integer, server_default="0")


class ImportJob(UUIDPk, Timestamps, Base):
    __tablename__ = "import_jobs"
    __table_args__ = {"schema": SCHEMA}

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.portfolios.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))  # zerodha_tradebook | groww | upstox | generic_csv | cas_pdf
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")  # pending | done | failed
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    errors: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AppliedCorporateAction(Base):
    """Idempotency for E35: each corporate action is applied to a portfolio at most once."""

    __tablename__ = "applied_corporate_actions"
    __table_args__ = {"schema": SCHEMA}

    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.portfolios.id", ondelete="CASCADE"), primary_key=True)
    corporate_action_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = {"schema": SCHEMA}

    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.portfolios.id", ondelete="CASCADE"), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    invested: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    market_value: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    is_complete: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class RealisedGain(UUIDPk, Timestamps, Base):
    """One sale lot from a broker's capital-gains / Tax P&L statement. Kept apart from the
    transaction ledger: current holdings come from holdings statements / CAS, so replaying these
    sales as SELL transactions would reduce the quantities a second time."""

    __tablename__ = "realised_gains"
    __table_args__ = (Index("ix_core_realised_gains_profile_fy", "profile_id", "fy"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"))
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.import_jobs.id", ondelete="CASCADE"), index=True)
    account_fp: Mapped[str | None] = mapped_column(String(64))
    broker: Mapped[str | None] = mapped_column(String(40))
    fy: Mapped[str] = mapped_column(String(7))                 # "2026-27"
    symbol: Mapped[str] = mapped_column(String(160))
    isin: Mapped[str | None] = mapped_column(String(12))
    asset: Mapped[str] = mapped_column(String(12))              # equity | equity_mf | debt | debt_mf | other | fno
    term: Mapped[str] = mapped_column(String(10))               # intraday | short | long | business
    buy_date: Mapped[date | None] = mapped_column(Date)
    sell_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    buy_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    sell_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    profit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    taxable_profit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    cost_override: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))  # the real cost, entered when the statement shows 0
    days_held: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(160), server_default="")
    flags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # the import that replaced this row (undo)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncomeEvent(UUIDPk, Timestamps, Base):
    """A dividend or interest payout from a broker statement."""

    __tablename__ = "income_events"
    __table_args__ = (Index("ix_core_income_events_profile_fy", "profile_id", "fy"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"))
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.import_jobs.id", ondelete="CASCADE"), index=True)
    account_fp: Mapped[str | None] = mapped_column(String(64))
    broker: Mapped[str | None] = mapped_column(String(40))
    fy: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(10))               # dividend | interest
    symbol: Mapped[str] = mapped_column(String(160))
    isin: Mapped[str | None] = mapped_column(String(12))
    ex_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    per_unit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HarvestMark(UUIDPk, Timestamps, Base):
    """A tax-harvesting action the person marked as done (sold / re-bought). Until a tax statement that
    contains the sale is imported, its effect is applied to the remaining savings so ideas aren't repeated."""

    __tablename__ = "harvest_marks"
    __table_args__ = (Index("ix_core_harvest_marks_profile_fy", "profile_id", "fy"), {"schema": SCHEMA})

    household_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.profiles.id", ondelete="CASCADE"))
    fy: Mapped[str] = mapped_column(String(7))
    key: Mapped[str] = mapped_column(String(80))                # "loss:<instrument>" | "gain:<instrument>"
    kind: Mapped[str] = mapped_column(String(8))                # loss | gain
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(160))
    name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    booked: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    st_part: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default="0")
    lt_part: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default="0")
    tax_saved: Mapped[Decimal] = mapped_column(Numeric(18, 2), server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
