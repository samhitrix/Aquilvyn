"""Ledger writes: validation, instrument resolution, idempotency, domain events."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings
from fm_common.crypto import encrypt, mask_pan, normalise_pan, pan_fingerprint
from fm_common.deps import Principal
from fm_common.events import publish

from .holdings import load_txns, to_txn
from .ledger import LedgerError, build_positions
from .models import (
    AccessLevel,
    Portfolio,
    PortfolioKind,
    Profile,
    ProfileAccess,
    Relationship,
    Transaction,
    TxnType,
)


async def resolve_instrument(principal: Principal, instrument_id: uuid.UUID | None, symbol: str | None, asset_type: str | None = None,
                             name: str | None = None, isin: str | None = None, sector: str | None = None) -> dict[str, Any]:
    if instrument_id:
        rows = await http.post(settings.market_url, "/api/v1/market/instruments/batch", token=principal.token, json={"ids": [str(instrument_id)]})
        if not rows:
            raise HTTPException(404, "Instrument not found")
        return rows[0]
    if not symbol:
        raise HTTPException(422, "instrument_id or symbol is required")
    body: dict[str, Any] = {"symbol": symbol}
    if asset_type:
        body["asset_type"] = asset_type
    if name:
        body["name"] = name[:200]
    if isin and len(isin) == 12:
        body["isin"] = isin
    if sector:
        body["sector"] = sector[:80]
    return await http.post(settings.market_url, "/api/v1/market/instruments/register", token=principal.token, json=body)


async def validate_ledger(db: AsyncSession, household_id: uuid.UUID, portfolio: Portfolio, extra: list[Transaction] | None = None, drop_id: uuid.UUID | None = None,
                          drop_ids: set[uuid.UUID] | None = None) -> None:
    """Replay the portfolio's ledger strictly: rejects sells that exceed holdings on that date."""
    dropped = (drop_ids or set()) | ({drop_id} if drop_id else set())
    txns = [t for t in await load_txns(db, household_id, [portfolio.id]) if t.id not in dropped] + (extra or [])
    try:
        build_positions([to_txn(t) for t in txns], strict=True)
    except LedgerError as exc:
        raise HTTPException(422, str(exc)) from exc


def gross_amount(txn_type: TxnType, qty: Decimal, price: Decimal, amount: Decimal | None) -> Decimal:
    if amount is not None and amount > 0:
        return amount
    if txn_type == TxnType.SPLIT:
        return Decimal(0)
    return (qty * price).quantize(Decimal("0.0001"))


async def record(
    db: AsyncSession, principal: Principal, portfolio: Portfolio, inst: dict[str, Any], *, txn_type: TxnType, trade_date: date,
    quantity: Decimal, price: Decimal, fees: Decimal = Decimal(0), amount: Decimal | None = None, notes: str = "",
    client_ref: str | None = None, source: str = "manual", validate: bool = True,
) -> tuple[Transaction, bool]:
    """Returns (transaction, created). Replaying the same ``client_ref`` returns the original row —
    this is what makes optimistic UI retries and offline re-sends safe."""
    if client_ref:
        existing = await db.scalar(select(Transaction).where(Transaction.portfolio_id == portfolio.id, Transaction.client_ref == client_ref))
        if existing and existing.deleted_at is None:
            return existing, False
        if existing:  # a deleted row (or deleted import) must not block re-adding the same row
            existing.client_ref = None
            await db.flush()
    if trade_date > date.today():
        raise HTTPException(422, "Trade date cannot be in the future")
    t = Transaction(
        id=uuid.uuid4(), household_id=principal.household_id, portfolio_id=portfolio.id, instrument_id=uuid.UUID(inst["id"]),
        symbol=inst["symbol"], asset_type=inst["asset_type"], txn_type=txn_type, trade_date=trade_date, quantity=quantity,
        price=price, fees=fees, amount=gross_amount(txn_type, quantity, price, amount), notes=notes, client_ref=client_ref,
        source=source, created_by=principal.user_id if not principal.is_service else None,
    )
    if validate and txn_type.removes_units:
        await validate_ledger(db, principal.household_id, portfolio, extra=[t])
    db.add(t)
    return t, True


async def announce_txn(principal: Principal, t: Transaction, profile_id: uuid.UUID, action: str = "recorded") -> None:
    await publish(
        f"txn.{action}",
        {"transaction_id": str(t.id), "portfolio_id": str(t.portfolio_id), "profile_id": str(profile_id),
         "instrument_id": str(t.instrument_id), "symbol": t.symbol, "txn_type": t.txn_type.value},
        household_id=str(principal.household_id),
    )


async def bootstrap_household(db: AsyncSession, household_id: uuid.UUID, user_id: uuid.UUID, full_name: str, relationship: Relationship = Relationship.SELF) -> Profile:
    """First login: a 'Self' profile for the member + default Broker, Mutual Funds, Retirement portfolios.

    Race-safe: the ``member.registered`` consumer and the first ``/profiles`` call can run at the
    same moment. The partial unique index ``uq_profiles_household_linked_user`` + ON CONFLICT DO
    NOTHING guarantee exactly one linked profile; the loser just re-reads the winner's row."""
    name = (full_name or "").strip() or "Me"
    live = (Profile.household_id == household_id, Profile.linked_user_id == user_id, Profile.deleted_at.is_(None))
    existing = await db.scalar(select(Profile).where(*live))
    if existing is None:
        new_id = uuid.uuid4()
        inserted = await db.scalar(
            insert(Profile).values(id=new_id, household_id=household_id, display_name=name, relationship=relationship,
                                   linked_user_id=user_id, color="#4F46E5")
            .on_conflict_do_nothing(index_elements=["household_id", "linked_user_id"],
                                    index_where=text("linked_user_id IS NOT NULL AND deleted_at IS NULL"))
            .returning(Profile.id)
        )
        if inserted is not None:
            await db.execute(insert(ProfileAccess).values(profile_id=new_id, user_id=user_id, level=AccessLevel.WRITE).on_conflict_do_nothing())
            for pname, kind in (("Stocks & ETFs", PortfolioKind.BROKER), ("Mutual Funds", PortfolioKind.MUTUAL_FUNDS), ("Retirement (EPF/PPF/NPS)", PortfolioKind.RETIREMENT)):
                db.add(Portfolio(id=uuid.uuid4(), household_id=household_id, profile_id=new_id, name=pname, kind=kind))
        await db.commit()
        existing = await db.scalar(select(Profile).where(*live).execution_options(populate_existing=True))
        assert existing is not None
        return existing
    if existing.display_name == "Me" and name != "Me":  # placeholder from a fallback path → real name
        existing.display_name = name
        await db.commit()
    return existing


async def set_pan(db: AsyncSession, p: Profile, pan: str) -> None:
    """Encrypt + fingerprint a PAN. A PAN can belong to only one profile in a household."""
    pan = normalise_pan(pan)
    fp = pan_fingerprint(pan)
    other = await db.scalar(select(Profile).where(Profile.household_id == p.household_id, Profile.pan_hash == fp,
                                                  Profile.id != p.id, Profile.deleted_at.is_(None)))
    if other is not None:
        raise HTTPException(409, f"PAN {mask_pan(pan)} already belongs to {other.display_name}")
    p.pan_encrypted, p.pan_last4, p.pan_hash = encrypt(pan, aad=str(p.id)), pan[-4:], fp


async def new_profile(db: AsyncSession, principal: Principal, fields: dict[str, Any], pan: str | None = None) -> Profile:
    """A family member profile + its default portfolios (no commit)."""
    p = Profile(id=uuid.uuid4(), household_id=principal.household_id, **fields)
    if pan:
        await set_pan(db, p, pan)
    db.add(p)
    await db.flush()
    db.add(ProfileAccess(profile_id=p.id, user_id=principal.user_id, level=AccessLevel.WRITE))
    kinds = [("Stocks & ETFs", PortfolioKind.BROKER), ("Mutual Funds", PortfolioKind.MUTUAL_FUNDS)]
    if fields.get("relationship", Relationship.OTHER) != Relationship.CHILD:
        kinds.append(("Retirement (EPF/PPF/NPS)", PortfolioKind.RETIREMENT))
    for name, kind in kinds:
        db.add(Portfolio(id=uuid.uuid4(), household_id=principal.household_id, profile_id=p.id, name=name, kind=kind))
    await db.flush()
    return p
