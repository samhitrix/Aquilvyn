from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from fm_common import http
from fm_common.config import settings
from fm_common.deps import DB, CurrentUser, Principal, require
from fm_common.events import publish
from fm_common.identity.rbac import P_IMPORT, P_INTERNAL, P_MEMBERS_MANAGE, P_PROFILE_READ_ANY, P_TXN_WRITE
from fm_common.logging import get_logger

from . import access, exposure, holdings, imports, service, taxstore
from .models import (
    AccessLevel,
    HoldingIntent,
    HoldingPref,
    ImportJob,
    Portfolio,
    PortfolioKind,
    PortfolioSnapshot,
    Profile,
    ProfileAccess,
    ProfileAccount,
    ProfileGroup,
    Relationship,
    RiskProfile,
    Transaction,
    TxnType,
)

log = get_logger(__name__)
router = APIRouter(tags=["portfolio"])


# ============================== Profiles (E08) ==============================
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_contact(field: str, v: str | None) -> str | None:
    """Both optional: blank → not set. Mobile keeps digits and a leading +; an Indian 10-digit number gets +91."""
    v = (v or "").strip()
    if not v:
        return None
    if field == "email":
        if not _EMAIL.match(v):
            raise ValueError("doesn't look like an email address")
        return v.lower()
    digits = re.sub(r"[^\d+]", "", v)
    bare = digits.lstrip("+")
    if not 7 <= len(bare) <= 15 or "+" in bare:
        raise ValueError("mobile number should have 10 digits (or a +country code)")
    if len(bare) == 10 and not digits.startswith("+"):
        return "+91" + bare
    return "+" + bare if digits.startswith("+") or len(bare) > 10 else bare


Email = Annotated[str | None, Field(max_length=254), BeforeValidator(lambda v: clean_contact("email", v))]
Mobile = Annotated[str | None, Field(max_length=20), BeforeValidator(lambda v: clean_contact("mobile", v))]


class ProfileIn(BaseModel):
    display_name: str = Field(max_length=120)
    relationship: Relationship = Relationship.OTHER
    pan: str | None = Field(default=None, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    date_of_birth: date | None = None
    email: Email = None
    mobile: Mobile = None
    tax_slab_pct: Decimal | None = Field(default=None, ge=0, le=42.744)
    risk_profile: RiskProfile = RiskProfile.MODERATE
    retirement_age: int = Field(default=60, ge=35, le=80)
    target_allocation: dict[str, float] = {}
    drawdown_thresholds: dict[str, float] = {}
    color: str | None = None


class ProfilePatch(BaseModel):
    display_name: str | None = None
    relationship: Relationship | None = None
    pan: str | None = Field(default=None, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    date_of_birth: date | None = None
    email: Email = None
    mobile: Mobile = None
    tax_slab_pct: Decimal | None = Field(default=None, ge=0, le=42.744)
    risk_profile: RiskProfile | None = None
    retirement_age: int | None = Field(default=None, ge=35, le=80)
    target_allocation: dict[str, float] | None = None
    drawdown_thresholds: dict[str, float] | None = None
    color: str | None = None


def profile_out(p: Profile) -> dict[str, Any]:
    return {
        "id": str(p.id), "display_name": p.display_name, "relationship": p.relationship.value,
        "pan_masked": f"XXXXXX{p.pan_last4}" if p.pan_last4 else None, "pan_tagged": bool(p.pan_hash), "date_of_birth": p.date_of_birth,
        "email": p.email, "mobile": p.mobile,
        "age": _age(p.date_of_birth), "tax_slab_pct": float(p.tax_slab_pct) if p.tax_slab_pct is not None else None,
        "risk_profile": p.risk_profile.value, "retirement_age": p.retirement_age, "target_allocation": p.target_allocation,
        "drawdown_thresholds": p.drawdown_thresholds, "color": p.color, "linked_user_id": str(p.linked_user_id) if p.linked_user_id else None,
        "version": p.version, "updated_at": p.updated_at,
    }


def _age(dob: date | None) -> int | None:
    if not dob:
        return None
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


async def _ensure_bootstrapped(db: DB, principal: Principal) -> None:
    """Fallback for when the ``member.registered`` event hasn't been processed yet. Uses the same
    race-safe bootstrap as the event consumer, with the member's real name from identity-svc."""
    if principal.is_service:
        return
    if await db.scalar(select(Profile.id).where(Profile.household_id == principal.household_id, Profile.deleted_at.is_(None)).limit(1)):
        return
    try:
        name = (await http.get(settings.identity_url, "/api/v1/auth/me", token=principal.token)).get("full_name") or "Me"
    except Exception:
        name = "Me"  # renamed to the real name when the event arrives
    await service.bootstrap_household(db, principal.household_id, principal.user_id, name)


async def _accounts_by_profile(db: DB, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, str]]]:
    out: dict[uuid.UUID, list[dict[str, str]]] = {}
    if ids:
        for a in (await db.execute(select(ProfileAccount).where(ProfileAccount.profile_id.in_(ids)).order_by(ProfileAccount.created_at))).scalars():
            out.setdefault(a.profile_id, []).append({"id": str(a.id), "kind": a.kind, "label": a.label})
    return out


@router.get("/profiles")
async def list_profiles(principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    await _ensure_bootstrapped(db, principal)
    ids = await access.visible_profile_ids(db, principal)
    rows = list((await db.execute(select(Profile).where(Profile.id.in_(ids)).order_by(Profile.created_at))).scalars()) if ids else []
    accts = await _accounts_by_profile(db, [p.id for p in rows])
    return [{**profile_out(p), "accounts": accts.get(p.id, [])} for p in rows]


@router.delete("/profiles/{profile_id}/accounts/{account_id}", status_code=204, response_class=Response, response_model=None)
async def unlink_account(profile_id: uuid.UUID, account_id: uuid.UUID, principal: CurrentUser, db: DB) -> None:
    await access.get_profile(db, principal, profile_id, write=True)
    await db.execute(ProfileAccount.__table__.delete().where(ProfileAccount.id == account_id, ProfileAccount.profile_id == profile_id))
    await db.commit()


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    if not principal.can("profile:write"):
        raise HTTPException(403, "Read-only members cannot add profiles")
    p = await service.new_profile(db, principal, body.model_dump(exclude={"pan"}), body.pan)
    await db.commit()
    await db.refresh(p)
    return profile_out(p)


@router.patch("/profiles/{profile_id}")
async def patch_profile(profile_id: uuid.UUID, body: ProfilePatch, principal: CurrentUser, db: DB) -> dict[str, Any]:
    p = await access.get_profile(db, principal, profile_id, write=True)
    old_name = p.display_name
    for k, v in body.model_dump(exclude_unset=True, exclude={"pan"}).items():
        setattr(p, k, v)
    if body.display_name is not None:
        p.display_name = body.display_name.strip() or old_name
    if body.pan:
        await service.set_pan(db, p, body.pan)
    await db.commit()
    await db.refresh(p)
    renamed = p.display_name != old_name
    if renamed and p.linked_user_id == principal.user_id:
        try:  # your own profile ⇄ your login name (shown in the sidebar and to other members)
            await http.call(settings.identity_url, "PATCH", "/api/v1/auth/me", token=principal.token, json={"full_name": p.display_name})
        except Exception as exc:
            log.warning("profile.login_rename_failed", error=str(exc))
    await publish("profile.updated", {"profile_id": str(p.id), **({"old_name": old_name, "new_name": p.display_name} if renamed else {})},
                  household_id=str(principal.household_id))
    return profile_out(p)


@router.delete("/profiles/{profile_id}", status_code=204, response_class=Response, response_model=None)
async def delete_profile(profile_id: uuid.UUID, principal: CurrentUser, db: DB) -> None:
    p = await access.get_profile(db, principal, profile_id, write=True)
    if p.linked_user_id == principal.user_id:
        raise HTTPException(400, "You cannot delete your own profile")
    p.deleted_at = datetime.now().astimezone()
    await db.commit()


class AccessIn(BaseModel):
    user_id: uuid.UUID
    level: AccessLevel | None  # None = revoke


@router.post("/profiles/{profile_id}/access")
async def grant_access(profile_id: uuid.UUID, body: AccessIn, db: DB, principal: Principal = require(P_MEMBERS_MANAGE)) -> dict[str, str]:
    await access.get_profile(db, principal, profile_id)
    if body.level is None:
        await db.execute(ProfileAccess.__table__.delete().where(ProfileAccess.profile_id == profile_id, ProfileAccess.user_id == body.user_id))
    else:
        stmt = insert(ProfileAccess).values(profile_id=profile_id, user_id=body.user_id, level=body.level)
        await db.execute(stmt.on_conflict_do_update(index_elements=["profile_id", "user_id"], set_={"level": body.level}))
    await db.commit()
    return {"status": "ok"}


# ============================== Groups ==============================
class GroupIn(BaseModel):
    name: str = Field(max_length=120)
    profile_ids: list[uuid.UUID]


@router.get("/groups")
async def list_groups(principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = (await db.execute(select(ProfileGroup).where(ProfileGroup.household_id == principal.household_id, ProfileGroup.deleted_at.is_(None)))).scalars()
    return [{"id": str(g.id), "name": g.name, "profile_ids": [str(x) for x in g.profile_ids]} for g in rows]


@router.post("/groups", status_code=201)
async def create_group(body: GroupIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    visible = set(await access.visible_profile_ids(db, principal))
    if not set(body.profile_ids) <= visible:
        raise HTTPException(404, "Unknown profile in group")
    g = ProfileGroup(id=uuid.uuid4(), household_id=principal.household_id, name=body.name, profile_ids=body.profile_ids)
    db.add(g)
    await db.commit()
    return {"id": str(g.id), "name": g.name, "profile_ids": [str(x) for x in g.profile_ids]}


@router.delete("/groups/{group_id}", status_code=204, response_class=Response, response_model=None)
async def delete_group(group_id: uuid.UUID, principal: CurrentUser, db: DB) -> None:
    g = await db.get(ProfileGroup, group_id)
    if g is None or g.household_id != principal.household_id:
        raise HTTPException(404, "Group not found")
    g.deleted_at = datetime.now().astimezone()
    await db.commit()


# ============================== Portfolios ==============================
class PortfolioIn(BaseModel):
    profile_id: uuid.UUID
    name: str = Field(max_length=120)
    kind: PortfolioKind = PortfolioKind.BROKER
    broker: str | None = None


def portfolio_out(p: Portfolio) -> dict[str, Any]:
    return {"id": str(p.id), "profile_id": str(p.profile_id), "name": p.name, "kind": p.kind.value, "broker": p.broker, "version": p.version}


@router.get("/portfolios")
async def list_portfolios(principal: CurrentUser, db: DB, profile_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    await _ensure_bootstrapped(db, principal)
    ids = await access.visible_profile_ids(db, principal)
    if profile_id:
        ids = [i for i in ids if i == profile_id]
    if not ids:
        return []
    rows = (await db.execute(select(Portfolio).where(Portfolio.profile_id.in_(ids), Portfolio.deleted_at.is_(None)).order_by(Portfolio.created_at))).scalars()
    return [portfolio_out(p) for p in rows]


@router.post("/portfolios", status_code=201)
async def create_portfolio(body: PortfolioIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    await access.get_profile(db, principal, body.profile_id, write=True)
    p = Portfolio(id=uuid.uuid4(), household_id=principal.household_id, **body.model_dump())
    db.add(p)
    await db.commit()
    return portfolio_out(p)


@router.delete("/portfolios/{portfolio_id}", status_code=204, response_class=Response, response_model=None)
async def delete_portfolio(portfolio_id: uuid.UUID, principal: CurrentUser, db: DB) -> None:
    p = await access.get_portfolio(db, principal, portfolio_id, write=True)
    p.deleted_at = datetime.now().astimezone()
    await db.commit()


@router.post("/portfolios/{portfolio_id}/clear")
async def clear_portfolio(portfolio_id: uuid.UUID, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    """Start a portfolio over: tombstones every transaction in it (kept in the audit log) and
    marks its imports as deleted, so the same files can be imported again. The portfolio stays."""
    pf = await access.get_portfolio(db, principal, portfolio_id, write=True)
    now = datetime.now().astimezone()
    txns = list((await db.execute(select(Transaction).where(Transaction.portfolio_id == pf.id, Transaction.deleted_at.is_(None)))).scalars())
    for t in txns:
        t.deleted_at = now
        t.client_ref = None
        t.version += 1
    for job in (await db.execute(select(ImportJob).where(ImportJob.portfolio_id == pf.id, ImportJob.status != "deleted"))).scalars():
        job.status = "deleted"
    await db.execute(PortfolioSnapshot.__table__.delete().where(PortfolioSnapshot.portfolio_id == pf.id))
    await db.commit()
    if txns:
        await publish("import.deleted", {"portfolio_id": str(pf.id), "profile_id": str(pf.profile_id), "deleted": len(txns), "cleared": True},
                      household_id=str(principal.household_id))
    return {"portfolio_id": str(pf.id), "deleted": len(txns)}


# ============================== Transactions (E05) ==============================
class TxnIn(BaseModel):
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID | None = None
    symbol: str | None = None
    asset_type: str | None = None
    txn_type: TxnType
    trade_date: date
    quantity: Decimal = Field(default=Decimal(0), ge=0)
    price: Decimal = Field(default=Decimal(0), ge=0)
    fees: Decimal = Field(default=Decimal(0), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=2000)
    client_ref: str | None = Field(default=None, max_length=80)


class TxnPatch(BaseModel):
    trade_date: date | None = None
    quantity: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    fees: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    version: int  # optimistic concurrency


def txn_out(t: Transaction) -> dict[str, Any]:
    return {
        "id": str(t.id), "portfolio_id": str(t.portfolio_id), "instrument_id": str(t.instrument_id), "symbol": t.symbol,
        "asset_type": t.asset_type, "txn_type": t.txn_type.value, "trade_date": t.trade_date.isoformat(),
        "quantity": float(t.quantity), "price": float(t.price), "fees": float(t.fees), "amount": float(t.amount),
        "notes": t.notes, "source": t.source, "client_ref": t.client_ref, "version": t.version, "created_at": t.created_at,
    }


@router.get("/transactions")
async def list_transactions(
    principal: CurrentUser, db: DB, portfolio_id: uuid.UUID | None = None, profile_id: uuid.UUID | None = None,
    instrument_id: uuid.UUID | None = None, limit: int = 100, before: date | None = None,
) -> list[dict[str, Any]]:
    ids = await access.visible_profile_ids(db, principal)
    if profile_id:
        ids = [i for i in ids if i == profile_id]
    pf_ids = list((await db.execute(select(Portfolio.id).where(Portfolio.profile_id.in_(ids), Portfolio.deleted_at.is_(None)))).scalars()) if ids else []
    if portfolio_id:
        pf_ids = [p for p in pf_ids if p == portfolio_id]
    if not pf_ids:
        return []
    stmt = select(Transaction).where(Transaction.portfolio_id.in_(pf_ids), Transaction.deleted_at.is_(None))
    if instrument_id:
        stmt = stmt.where(Transaction.instrument_id == instrument_id)
    if before:
        stmt = stmt.where(Transaction.trade_date < before)
    rows = (await db.execute(stmt.order_by(Transaction.trade_date.desc(), Transaction.created_at.desc()).limit(min(limit, 1000)))).scalars()
    return [txn_out(t) for t in rows]


@router.post("/transactions", status_code=201)
async def create_transaction(body: TxnIn, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    pf = await access.get_portfolio(db, principal, body.portfolio_id, write=True)
    inst = await service.resolve_instrument(principal, body.instrument_id, body.symbol, body.asset_type)
    t, created = await service.record(
        db, principal, pf, inst, txn_type=body.txn_type, trade_date=body.trade_date, quantity=body.quantity, price=body.price,
        fees=body.fees, amount=body.amount, notes=body.notes, client_ref=body.client_ref,
    )
    await db.commit()
    if created:
        await service.announce_txn(principal, t, pf.profile_id)
    return txn_out(t)


@router.patch("/transactions/{txn_id}")
async def patch_transaction(txn_id: uuid.UUID, body: TxnPatch, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    t = await db.get(Transaction, txn_id)
    if t is None or t.household_id != principal.household_id or t.deleted_at:
        raise HTTPException(404, "Transaction not found")
    pf = await access.get_portfolio(db, principal, t.portfolio_id, write=True)
    if body.version != t.version:
        raise HTTPException(409, "Transaction was changed elsewhere; reload and retry")
    for k, v in body.model_dump(exclude_unset=True, exclude={"version"}).items():
        setattr(t, k, v)
    if body.amount is None and (body.quantity is not None or body.price is not None):
        t.amount = service.gross_amount(t.txn_type, t.quantity, t.price, None)
    t.version += 1
    await db.flush()
    await service.validate_ledger(db, principal.household_id, pf)
    await db.commit()
    await service.announce_txn(principal, t, pf.profile_id, "updated")
    return txn_out(t)


@router.delete("/transactions/{txn_id}", status_code=204, response_class=Response, response_model=None)
async def delete_transaction(txn_id: uuid.UUID, db: DB, principal: Principal = require(P_TXN_WRITE)) -> None:
    t = await db.get(Transaction, txn_id)
    if t is None or t.household_id != principal.household_id or t.deleted_at:
        raise HTTPException(404, "Transaction not found")
    pf = await access.get_portfolio(db, principal, t.portfolio_id, write=True)
    await service.validate_ledger(db, principal.household_id, pf, drop_id=t.id)
    t.deleted_at = datetime.now().astimezone()  # tombstone (audit + future delta-sync)
    t.version += 1
    await db.commit()
    await service.announce_txn(principal, t, pf.profile_id, "deleted")


# ============================== Holdings (E06) ==============================
@router.get("/holdings")
async def get_holdings(principal: CurrentUser, db: DB, scope: str = "household", id: uuid.UUID | None = None, price_wait: float = 0) -> dict[str, Any]:
    """scope = household | profile | group | portfolio"""
    visible = await access.visible_profile_ids(db, principal)
    profile_ids = visible
    portfolio_filter: uuid.UUID | None = None
    if scope == "profile":
        profile_ids = [p for p in visible if p == id]
    elif scope == "group":
        g = await db.get(ProfileGroup, id) if id else None
        if g is None or g.household_id != principal.household_id:
            raise HTTPException(404, "Group not found")
        profile_ids = [p for p in g.profile_ids if p in visible]
    elif scope == "portfolio":
        pf = await access.get_portfolio(db, principal, id) if id else None
        if pf is None:
            raise HTTPException(404, "Portfolio not found")
        profile_ids, portfolio_filter = [pf.profile_id], pf.id
    elif scope != "household":
        raise HTTPException(422, "Unknown scope")
    if not profile_ids:
        return {"as_of": date.today().isoformat(), "summary": None, "profiles": [], "holdings": []}
    stmt = select(Portfolio).where(Portfolio.profile_id.in_(profile_ids), Portfolio.deleted_at.is_(None))
    if portfolio_filter:
        stmt = stmt.where(Portfolio.id == portfolio_filter)
    portfolios = list((await db.execute(stmt)).scalars())
    profiles = {p.id: p for p in (await db.execute(select(Profile).where(Profile.id.in_(profile_ids)))).scalars()}
    result = await holdings.compute(db, principal, portfolios, profiles, min(price_wait, 25.0))
    result["scope"] = {"type": scope, "id": str(id) if id else None}
    if result.get("summary") is not None and not portfolio_filter:  # this FY's realised gains / dividends from tax statements
        from .models import IncomeEvent, RealisedGain
        from .tax import fy_of

        fy = fy_of(date.today())
        rg = (await db.execute(select(RealisedGain.taxable_profit, RealisedGain.sell_value, RealisedGain.cost_override).where(
            RealisedGain.household_id == principal.household_id, RealisedGain.profile_id.in_(profile_ids), RealisedGain.fy == fy,
            RealisedGain.deleted_at.is_(None)))).all()
        inc = await db.scalar(select(func.coalesce(func.sum(IncomeEvent.amount), 0)).where(
            IncomeEvent.household_id == principal.household_id, IncomeEvent.profile_id.in_(profile_ids), IncomeEvent.fy == fy,
            IncomeEvent.deleted_at.is_(None)))
        if rg or inc:
            result["summary"].update({"fy": fy, "fy_realised": round(sum(float(s - c) if c is not None else float(t) for t, s, c in rg), 2),
                                      "fy_income": round(float(inc or 0), 2)})
    result["profile_settings"] = {str(k): profile_out(v) for k, v in profiles.items()}
    return result


@router.get("/holdings/exposure")
async def holdings_exposure(principal: CurrentUser, db: DB, scope: str = "household", id: uuid.UUID | None = None,
                            basis: str = "current", lookthrough: bool = True, asset: str = "all") -> dict[str, Any]:
    """Analytics: asset class, product, sector and large/mid/small split for the family or a profile."""
    h = await get_holdings(principal, db, scope, id)
    return await exposure.exposure(principal, h["holdings"], basis="invested" if basis == "invested" else "current",
                                   lookthrough=lookthrough, asset=asset)


class PrefIn(BaseModel):
    profile_id: uuid.UUID
    instrument_id: uuid.UUID
    intent: HoldingIntent | None = None
    goal_name: str | None = None
    thesis: str | None = None
    thesis_checks: list[dict[str, Any]] | None = None
    drawdown_threshold_pct: Decimal | None = Field(default=None, gt=0, le=90)
    stop_price: Decimal | None = Field(default=None, ge=0)
    target_price: Decimal | None = Field(default=None, ge=0)


def pref_out(p: HoldingPref) -> dict[str, Any]:
    return {
        "profile_id": str(p.profile_id), "instrument_id": str(p.instrument_id), "intent": p.intent.value if p.intent else None,
        "intent_source": p.intent_source, "goal_name": p.goal_name, "thesis": p.thesis, "thesis_checks": p.thesis_checks,
        "drawdown_threshold_pct": float(p.drawdown_threshold_pct) if p.drawdown_threshold_pct is not None else None,
        "stop_price": float(p.stop_price) if p.stop_price is not None else None,
        "target_price": float(p.target_price) if p.target_price is not None else None,
    }


@router.get("/holding-prefs")
async def list_prefs(principal: CurrentUser, db: DB, profile_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    ids = await access.visible_profile_ids(db, principal)
    if profile_id:
        ids = [i for i in ids if i == profile_id]
    if not ids:
        return []
    rows = (await db.execute(select(HoldingPref).where(HoldingPref.profile_id.in_(ids), HoldingPref.deleted_at.is_(None)))).scalars()
    return [pref_out(p) for p in rows]


@router.put("/holding-prefs")
async def upsert_pref(body: PrefIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """User override (intent_source='user') — or the Advisor's auto-classification when called
    with a service token (intent_source='auto', never overwrites a user's choice)."""
    if not principal.is_service:
        await access.get_profile(db, principal, body.profile_id, write=True)
    p = await db.scalar(select(HoldingPref).where(HoldingPref.profile_id == body.profile_id, HoldingPref.instrument_id == body.instrument_id))
    source = "auto" if principal.is_service else "user"
    if p is None:
        p = HoldingPref(id=uuid.uuid4(), household_id=principal.household_id, profile_id=body.profile_id, instrument_id=body.instrument_id)
        db.add(p)
    elif p.household_id != principal.household_id:
        raise HTTPException(404, "Not found")
    if source == "auto" and p.intent_source == "user" and p.intent is not None:
        return pref_out(p)
    for k, v in body.model_dump(exclude_unset=True, exclude={"profile_id", "instrument_id"}).items():
        setattr(p, k, v)
    if "intent" in body.model_fields_set:
        p.intent_source = source
    await db.commit()
    await publish("holding.prefs_changed", {"profile_id": str(body.profile_id), "instrument_id": str(body.instrument_id)}, household_id=str(principal.household_id))
    return pref_out(p)


# ============================== Imports (E07) ==============================
@router.post("/imports/preview")
async def import_preview(
    db: DB, principal: Principal = require(P_IMPORT), file: UploadFile = File(...),
    kind: str | None = Form(None), password: str | None = Form(None), profile_id: str | None = Form(None),
    mapping: str | None = Form(None),
) -> dict[str, Any]:
    """Step 1: read the file, find whose it is (PAN / broker account), and propose a profile per owner.
    ``profile_id``: who you're importing for (used where the file doesn't identify its owner).
    ``mapping``: JSON ``{"kind": "trades"|"holdings", "fields": {field: column header}, "label": …}`` for a layout
    Aquilvyn didn't recognise (the 422 response offers the columns); it is remembered for next time."""
    import json

    try:
        mp = json.loads(mapping) if mapping else None
    except ValueError as exc:
        raise HTTPException(422, "mapping must be JSON") from exc
    return await imports.preview(db, principal, file.filename or "upload", await file.read(), password, kind, profile_id or None, mp)


@router.get("/imports/templates")
async def import_templates(db: DB, principal: Principal = require(P_IMPORT)) -> list[dict[str, Any]]:
    """File layouts whose columns you mapped once (reused automatically)."""
    from .models import ImportTemplate

    rows = (await db.execute(select(ImportTemplate).where(ImportTemplate.household_id == principal.household_id)
                             .order_by(ImportTemplate.updated_at.desc()))).scalars()
    return [{"id": str(t.id), "label": t.label, "kind": t.kind, "mapping": t.mapping, "uses": t.uses, "updated_at": t.updated_at} for t in rows]


@router.delete("/imports/templates/{template_id}", status_code=204, response_class=Response, response_model=None)
async def delete_import_template(template_id: uuid.UUID, db: DB, principal: Principal = require(P_IMPORT)) -> None:
    from .models import ImportTemplate

    t = await db.get(ImportTemplate, template_id)
    if t is not None and t.household_id == principal.household_id:
        await db.delete(t)
        await db.commit()


class PlanIn(BaseModel):
    token: str
    key: str
    profile_id: str
    mode: str = "replace"
    portfolio_id: str | None = None


@router.post("/imports/plan")
async def import_plan(body: PlanIn, db: DB, principal: Principal = require(P_IMPORT)) -> dict[str, Any]:
    """Step 1½: exactly what importing this owner's rows into ``profile_id`` would change (nothing is written)."""
    return await imports.plan(db, principal, body.token, body.key, body.profile_id, body.mode, body.portfolio_id)


class CommitIn(BaseModel):
    token: str
    mode: str = "replace"  # holdings statements: replace (become exactly the file) | update (only the listed holdings)
    # group key → {"profile_id": …} | {"create": "Name", "relationship": "spouse"} | {"skip": true}; optional "portfolio_id"
    choices: dict[str, dict[str, Any]] = {}


@router.post("/imports/commit", status_code=201)
async def import_commit(body: CommitIn, db: DB, principal: Principal = require(P_IMPORT)) -> dict[str, Any]:
    """Step 2: write each owner's rows into their profile (refuses a PAN that belongs to someone else)."""
    return await imports.commit(db, principal, body.token, body.choices, body.mode)


@router.post("/imports", status_code=201)
async def run_import(
    db: DB, principal: Principal = require(P_IMPORT), portfolio_id: uuid.UUID = Form(...), file: UploadFile = File(...),
    kind: str | None = Form(None), password: str | None = Form(None), mode: str = Form("replace"),
) -> dict[str, Any]:
    """One-shot import (scripts / older clients): owners the file identifies (PAN, broker account) go to
    their matched profile; anything unmatched goes to ``portfolio_id``'s profile — with the same PAN
    safety checks as the review flow."""
    pf = await access.get_portfolio(db, principal, portfolio_id, write=True)
    pv = await imports.preview(db, principal, file.filename or "upload", await file.read(), password, kind)
    choices: dict[str, dict[str, Any]] = {}
    for g in pv["groups"]:
        owner = (g.get("matched_profile") or {}).get("id") or str(pf.profile_id)
        choices[g["key"]] = {"profile_id": owner, **({"portfolio_id": str(pf.id)} if owner == str(pf.profile_id) and pv["kind"] != "holdings" else {})}
    res = await imports.commit(db, principal, pv["token"], choices, mode)
    return {**res["jobs"][0], "jobs": res["jobs"]}


@router.get("/imports")
async def list_imports(principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = (await db.execute(select(ImportJob).where(ImportJob.household_id == principal.household_id).order_by(ImportJob.created_at.desc()).limit(50))).scalars()
    return [{"id": str(j.id), "portfolio_id": str(j.portfolio_id), "kind": j.kind, "filename": j.filename, "status": j.status,
             "stats": j.stats, "errors": j.errors[:20], "created_at": j.created_at} for j in rows]


@router.delete("/imports/{job_id}")
async def delete_import(job_id: uuid.UUID, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    """Undo a whole import: tombstones every transaction that file created, in one go. Refused
    if a later transaction (e.g. a manual sell) depends on units that only the import brought in."""
    job = await db.get(ImportJob, job_id)
    if job is None or job.household_id != principal.household_id:
        raise HTTPException(404, "Import not found")
    pf = await access.get_portfolio(db, principal, job.portfolio_id, write=True)
    if job.kind == "tax_pnl":  # realised lots + dividends: removed, and whatever this statement replaced comes back
        res = await taxstore.undo(db, job)
        job.status = "deleted"
        job.stats = {**(job.stats or {}), "deleted": res["deleted"], "restored_on_delete": res["restored"]}
        await taxstore.refresh_statuses(db, principal.household_id)
        await db.commit()
        return {"id": str(job.id), "status": job.status, **res}
    # a holdings statement can write into several portfolios (stocks + funds) — match by source
    txns = list((await db.execute(select(Transaction).where(
        Transaction.household_id == principal.household_id, Transaction.source == f"import:{job.id}", Transaction.deleted_at.is_(None),
    ))).scalars())
    for pid in {t.portfolio_id for t in txns}:
        tpf = pf if pid == pf.id else await access.get_portfolio(db, principal, pid, write=True)
        try:
            await service.validate_ledger(db, principal.household_id, tpf, drop_ids={t.id for t in txns})
        except HTTPException as exc:
            raise HTTPException(409, f"Can't remove this import: later transactions depend on it ({exc.detail}). Delete those first.") from exc
    now = datetime.now().astimezone()
    for t in txns:
        t.deleted_at = now
        t.client_ref = None  # so the same file can be imported again later
        t.version += 1
    # a holdings import also brings back what it replaced (true undo)
    back = [uuid.UUID(i) for i in (job.stats or {}).get("replaced_ids", [])]
    restored = 0
    if back:
        for t in (await db.execute(select(Transaction).where(Transaction.id.in_(back), Transaction.deleted_at.is_not(None)))).scalars():
            t.deleted_at, t.version = None, t.version + 1
            restored += 1
        await _refresh_job_statuses(db, principal.household_id)
    job.status = "deleted"
    job.stats = {**(job.stats or {}), "deleted": len(txns), "restored_on_delete": restored}
    await db.commit()
    if txns:
        await publish("import.deleted", {"job_id": str(job.id), "portfolio_id": str(pf.id), "profile_id": str(pf.profile_id), "deleted": len(txns)},
                      household_id=str(principal.household_id))
    return {"id": str(job.id), "status": job.status, "deleted": len(txns), "restored": restored}


async def _refresh_job_statuses(db: DB, household_id: uuid.UUID) -> None:
    """An import is 'done' while any of its rows is live, 'replaced' when all were replaced."""
    await db.flush()
    jobs = (await db.execute(select(ImportJob).where(ImportJob.household_id == household_id, ImportJob.status.in_(["done", "replaced"]),
                                                     ImportJob.kind != "tax_pnl"))).scalars()  # tax imports: taxstore.refresh_statuses
    for j in jobs:
        live = await db.scalar(select(Transaction.id).where(Transaction.source == f"import:{j.id}", Transaction.deleted_at.is_(None)).limit(1))
        j.status = "done" if live else "replaced"


@router.post("/imports/{job_id}/restore")
async def restore_import(job_id: uuid.UUID, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    """Bring back the rows of a replaced (or deleted) import. The restored import becomes the truth for
    its instruments: current rows of those instruments in the same profile's portfolios of that kind
    are replaced (kept, not deleted — deleting/restoring again reverses it), so nothing is doubled."""
    job = await db.get(ImportJob, job_id)
    if job is None or job.household_id != principal.household_id:
        raise HTTPException(404, "Import not found")
    if job.kind == "tax_pnl":
        await access.get_portfolio(db, principal, job.portfolio_id, write=True)
        res = await taxstore.restore(db, job)
        if not res["restored"]:
            raise HTTPException(409, "Nothing to restore — this import's rows are already live.")
        job.status = "done"
        await db.commit()
        return {"id": str(job.id), "status": "done", **res}
    rows = list((await db.execute(select(Transaction).where(Transaction.source == f"import:{job.id}", Transaction.deleted_at.is_not(None)))).scalars())
    if not rows:
        raise HTTPException(409, "Nothing to restore — this import's rows are already live (or it added none).")
    now = datetime.now().astimezone()
    superseded: list[str] = []
    by_pf: dict[uuid.UUID, set[str]] = {}
    for t in rows:
        by_pf.setdefault(t.portfolio_id, set()).add(t.symbol)
    checked: list[Portfolio] = []
    for pid, syms in by_pf.items():
        pf = await access.get_portfolio(db, principal, pid, write=True)
        same_kind = [p.id for p in (await db.execute(select(Portfolio).where(Portfolio.profile_id == pf.profile_id, Portfolio.kind == pf.kind,
                                                                              Portfolio.deleted_at.is_(None)))).scalars()]
        for t in (await db.execute(select(Transaction).where(Transaction.portfolio_id.in_(same_kind), Transaction.symbol.in_(syms),
                                                             Transaction.deleted_at.is_(None)))).scalars():
            t.deleted_at, t.version = now, t.version + 1
            superseded.append(str(t.id))
        checked.append(pf)
    for t in rows:
        if t.client_ref and await db.scalar(select(Transaction.id).where(Transaction.portfolio_id == t.portfolio_id, Transaction.client_ref == t.client_ref,
                                                                          Transaction.deleted_at.is_(None), Transaction.id != t.id).limit(1)):
            t.client_ref = None  # an identical row was re-imported meanwhile and is live — it was just superseded above
        t.deleted_at, t.version = None, t.version + 1
    await db.flush()
    for pf in checked:
        try:
            await service.validate_ledger(db, principal.household_id, pf)
        except HTTPException as exc:
            raise HTTPException(409, f"Can't restore: the ledger would be inconsistent ({exc.detail}).") from exc
    job.stats = {**(job.stats or {}), "replaced_ids": [*(job.stats or {}).get("replaced_ids", []), *superseded], "restored": len(rows)}
    await _refresh_job_statuses(db, principal.household_id)
    job.status = "done"
    await db.commit()
    await publish("import.completed", {"job_id": str(job.id), "portfolio_id": str(job.portfolio_id), "restored": len(rows)},
                  household_id=str(principal.household_id))
    return {"id": str(job.id), "status": job.status, "restored": len(rows), "superseded": len(superseded)}


# ============================== Performance ==============================
@router.get("/performance")
async def performance(principal: CurrentUser, db: DB, profile_id: uuid.UUID | None = None, days: int = 365,
                      profile_ids: str | None = None) -> list[dict[str, Any]]:
    ids = await access.visible_profile_ids(db, principal)
    if profile_id:
        ids = [i for i in ids if i == profile_id]
    if profile_ids:  # a group / selection: comma-separated profile ids
        wanted = {x.strip() for x in profile_ids.split(",") if x.strip()}
        ids = [i for i in ids if str(i) in wanted]
    if not ids:
        return []
    from sqlalchemy import func

    since = date.fromordinal(date.today().toordinal() - days)
    stmt = (
        select(PortfolioSnapshot.snapshot_date, func.sum(PortfolioSnapshot.invested), func.sum(PortfolioSnapshot.market_value))
        .join(Portfolio, Portfolio.id == PortfolioSnapshot.portfolio_id)
        .where(Portfolio.profile_id.in_(ids), PortfolioSnapshot.snapshot_date >= since)
        .group_by(PortfolioSnapshot.snapshot_date).order_by(PortfolioSnapshot.snapshot_date)
    )
    return [{"date": d.isoformat(), "invested": float(i), "market_value": float(v)} for d, i, v in await db.execute(stmt)]


# ============================== Internal ==============================
@router.get("/internal/households")
async def active_households(db: DB, principal: Principal = require(P_INTERNAL)) -> list[str]:
    """Households with at least one live transaction — the nightly advisor run iterates these."""
    rows = await db.execute(select(Transaction.household_id).where(Transaction.deleted_at.is_(None)).distinct())
    return [str(r[0]) for r in rows]


@router.get("/internal/profiles")
async def internal_profiles(db: DB, principal: Principal = require(P_PROFILE_READ_ANY)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Profile).where(Profile.household_id == principal.household_id, Profile.deleted_at.is_(None)))).scalars()
    return [profile_out(p) for p in rows]
