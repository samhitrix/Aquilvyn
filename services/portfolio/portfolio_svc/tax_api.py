"""/tax — realised gains, dividends and the estimated tax per person and for the family."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from fm_common.deps import DB, CurrentUser, Principal, require
from fm_common.identity.rbac import P_TXN_WRITE

from . import access, tax
from .models import HarvestMark, ImportJob, IncomeEvent, Portfolio, Profile, ProfileGroup, RealisedGain

router = APIRouter(tags=["tax"])


async def _profiles(db: DB, principal: Principal, scope: str, id: uuid.UUID | None) -> list[Profile]:
    visible = await access.visible_profile_ids(db, principal)
    if scope == "profile":
        ids = [p for p in visible if p == id]
    elif scope == "group":
        grp = await db.get(ProfileGroup, id) if id else None
        if grp is None or grp.household_id != principal.household_id:
            raise HTTPException(404, "Group not found")
        ids = [p for p in grp.profile_ids if p in visible]
    elif scope == "household":
        ids = visible
    else:
        raise HTTPException(422, "Unknown scope")
    if not ids:
        return []
    return list((await db.execute(select(Profile).where(Profile.id.in_(ids), Profile.deleted_at.is_(None)).order_by(Profile.created_at))).scalars())


def _gain_out(g: RealisedGain, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {"id": str(g.id), "profile_id": str(g.profile_id), "profile_name": names.get(g.profile_id), "symbol": g.symbol, "isin": g.isin,
            "asset": g.asset, "term": g.term, "buy_date": g.buy_date, "sell_date": g.sell_date, "quantity": float(g.quantity),
            "buy_value": tax.money(g.buy_value), "sell_value": tax.money(g.sell_value), "profit": tax.money(g.profit),
            "taxable_profit": tax.money(g.taxable_profit), "cost_override": None if g.cost_override is None else tax.money(g.cost_override),
            "effective_gain": round(tax.effective_gain(_gdict(g)), 2), "days_held": g.days_held, "flags": g.flags, "broker": g.broker,
            "section": g.section}


def _gdict(g: RealisedGain) -> dict[str, Any]:
    return {"asset": g.asset, "term": g.term, "sell_date": g.sell_date, "taxable_profit": g.taxable_profit, "sell_value": g.sell_value,
            "cost_override": g.cost_override}


async def _rows(db: DB, principal: Principal, profiles: list[Profile], fy: str | None) -> tuple[list[RealisedGain], list[IncomeEvent]]:
    ids = [p.id for p in profiles]
    if not ids:
        return [], []
    gq = select(RealisedGain).where(RealisedGain.household_id == principal.household_id, RealisedGain.profile_id.in_(ids), RealisedGain.deleted_at.is_(None))
    iq = select(IncomeEvent).where(IncomeEvent.household_id == principal.household_id, IncomeEvent.profile_id.in_(ids), IncomeEvent.deleted_at.is_(None))
    if fy:
        gq, iq = gq.where(RealisedGain.fy == fy), iq.where(IncomeEvent.fy == fy)
    gains = list((await db.execute(gq.order_by(RealisedGain.sell_date.desc().nulls_last(), RealisedGain.symbol))).scalars())
    income = list((await db.execute(iq.order_by(IncomeEvent.ex_date.desc().nulls_last()))).scalars())
    return gains, income


def _variants(sym: str) -> list[str]:
    b = _base(sym)
    return [b, f"{b}.NS", f"{b}.BO"]


async def auto_resolve(db: DB, principal: Principal, gains: list[RealisedGain]) -> int:
    """Zero-cost lots, resolved from data we already have (each lot checked once):
    1. a bonus / split near the lot's date → bonus shares: ₹0 cost is correct by law, nothing to enter;
    2. otherwise the cost per unit of the same stock bought earlier (other lots on the statement, then the
       ledger) → filled in as an estimate the person can change;
    3. otherwise it stays for the person to enter (or mark as a bonus)."""
    todo = [g for g in gains if "zero_cost" in (g.flags or []) and g.cost_override is None and "cost_checked" not in (g.flags or [])]
    if not todo:
        return 0
    from decimal import Decimal

    from fm_common import http
    from fm_common.config import settings as common

    from .models import Transaction, TxnType

    try:
        near = await http.post(common.market_url, "/api/v1/market/corporate-actions/near", token=principal.token,
                               json={"items": [{"symbol": g.symbol, "isin": g.isin, "date": (g.buy_date or g.sell_date).isoformat()} for g in todo]})
    except Exception:
        near = [None] * len(todo)
    for g, ca in zip(todo, near or [None] * len(todo), strict=False):
        flags = [f for f in (g.flags or []) if f != "zero_cost"] + ["cost_checked"]
        if ca:
            ratio = f"{ca.get('ratio_to') or '?'}:{ca.get('ratio_from') or '?'}"
            g.flags = [*flags, f"bonus:{ca['action_type']} {ratio} ex {ca['ex_date']}"]
            continue
        same = [x for x in gains if x.id != g.id and x.profile_id == g.profile_id and x.buy_value > 0 and x.quantity > 0
                and (_base(x.symbol) == _base(g.symbol) or (g.isin and x.isin == g.isin))
                and (not g.buy_date or not x.buy_date or x.buy_date <= g.buy_date)]
        per, src = None, None
        if same:
            per, src = sum(x.buy_value for x in same) / sum(x.quantity for x in same), "your earlier lots on the statement"
        else:
            pf_ids = [p.id for p in (await db.execute(select(Portfolio).where(Portfolio.profile_id == g.profile_id))).scalars()]
            buys = (await db.execute(select(Transaction.quantity, Transaction.price).where(
                Transaction.portfolio_id.in_(pf_ids or [uuid.uuid4()]), Transaction.symbol.in_(_variants(g.symbol)),
                Transaction.txn_type.in_([TxnType.BUY, TxnType.SIP]), Transaction.deleted_at.is_(None)))).all()
            qty = sum((q for q, _ in buys), Decimal(0))
            if qty > 0:
                per, src = sum((q * p for q, p in buys), Decimal(0)) / qty, "your holdings / transactions"
        if per:
            g.cost_override = (per * g.quantity).quantize(Decimal("0.01"))
            g.flags = [*flags, "zero_cost", f"cost_estimated:{src}"]
        else:
            g.flags = [*flags, "zero_cost"]
    await db.commit()
    return len(todo)


@router.get("/tax/summary")
async def summary(principal: CurrentUser, db: DB, fy: str | None = None, scope: str = "household", id: uuid.UUID | None = None,
                  with_plan: bool = True) -> dict[str, Any]:
    """``with_plan=false``: the raw tax-free room, without the advisor's fund plan taken off (the advisor itself asks
    for that, to phase the plan within it)."""
    profiles = await _profiles(db, principal, scope, id)
    current = tax.fy_of(date.today())
    all_gains, all_income = await _rows(db, principal, profiles, None)
    if await auto_resolve(db, principal, all_gains):
        all_gains, all_income = await _rows(db, principal, profiles, None)
    fys = sorted({g.fy for g in all_gains} | {i.fy for i in all_income} | {current}, reverse=True)
    fy = fy or next((f for f in fys if any(g.fy == f for g in all_gains) or any(i.fy == f for i in all_income)), current)
    names = {p.id: p.display_name for p in profiles}
    holdings_rows: list[dict[str, Any]] = []
    if fy == current:
        from .api import get_holdings

        try:
            holdings_rows = (await get_holdings(principal, db, scope, id)).get("holdings", [])
        except HTTPException:
            holdings_rows = []
    marks = list((await db.execute(select(HarvestMark).where(
        HarvestMark.household_id == principal.household_id, HarvestMark.profile_id.in_([p.id for p in profiles] or [uuid.uuid4()]),
        HarvestMark.fy == fy, HarvestMark.deleted_at.is_(None)).order_by(HarvestMark.created_at.desc()))).scalars())
    plan_sales = await _plan_sales(principal, fy) if fy == current and with_plan else []
    people: list[dict[str, Any]] = []
    for p in profiles:
        g = [_gdict(x) for x in all_gains if x.profile_id == p.id and x.fy == fy]
        inc = [{"kind": x.kind, "amount": x.amount} for x in all_income if x.profile_id == p.id and x.fy == fy]
        mine = [h for h in holdings_rows if h.get("profile_id") == str(p.id) and h.get("quantity", 0) > 0]
        if not g and not inc and scope != "profile" and not mine:
            continue
        s = tax.compute(fy, g, inc, float(p.tax_slab_pct) if p.tax_slab_pct is not None else None)
        done = [_mark_out(m, all_gains) for m in marks if m.profile_id == p.id]
        planned = {x["instrument_id"]: x["ltcg"] for x in plan_sales if x["profile_id"] == str(p.id)}
        s["harvest"] = tax.harvest(fy, s, mine, done=done, planned=planned) if fy == current else None
        s["harvest_done"] = done
        people.append({"profile_id": str(p.id), "name": p.display_name, "has_statement": bool(g or inc), **s})
    total = {k: round(sum(x[k] for x in people), 2) for k in ("realised", "dividends", "interest", "estimated_tax")}
    ids = [p.id for p in profiles]
    jobs = list((await db.execute(select(ImportJob, Portfolio.profile_id).join(Portfolio, Portfolio.id == ImportJob.portfolio_id).where(
        ImportJob.household_id == principal.household_id, ImportJob.kind == "tax_pnl", ImportJob.status == "done",
        Portfolio.profile_id.in_(ids) if ids else Portfolio.profile_id.is_(None)).order_by(ImportJob.created_at.desc()))).all())
    zero_cost = [_gain_out(x, names) for x in all_gains if x.fy == fy
                 and ("zero_cost" in (x.flags or []) or any(str(f).startswith("bonus") for f in x.flags or []))]
    return {
        "fy": fy, "fys": fys, "current_fy": current, "people": people, "total": total,
        "statements": [{"id": str(j.id), "profile_id": str(pid), "profile_name": names.get(pid), "filename": j.filename,
                        "broker": (j.stats or {}).get("broker"), "period": (j.stats or {}).get("period"),
                        "checks_ok": sum(1 for c in (j.stats or {}).get("checks") or [] if c.get("ok")),
                        "checks_total": len((j.stats or {}).get("checks") or []), "imported_at": j.created_at} for j, pid in jobs],
        "zero_cost": zero_cost,
        "disclaimer": "Estimate from the investment statements you imported — not salary, other income, deductions or TDS. "
                      "Surcharge (income above ₹50 L) isn't included. Check with a tax professional before filing.",
    }


async def _plan_sales(principal: Principal, fy: str) -> list[dict[str, Any]]:
    """Fund-plan sales the advisor schedules this financial year (their long-term gain uses the tax-free room)."""
    from fm_common import http
    from fm_common.config import settings as common

    try:
        return await http.get(common.advisor_url, "/api/v1/advisor/fund-plan/tax", token=principal.token, params={"fy": fy}, request_timeout=10) or []
    except Exception:  # advisor down: harvesting still works, just without the plan taken off
        return []


@router.get("/tax/realised")
async def realised(principal: CurrentUser, db: DB, fy: str | None = None, scope: str = "household", id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    profiles = await _profiles(db, principal, scope, id)
    gains, _ = await _rows(db, principal, profiles, fy or tax.fy_of(date.today()))
    names = {p.id: p.display_name for p in profiles}
    return [_gain_out(g, names) for g in gains]


@router.get("/tax/income")
async def income(principal: CurrentUser, db: DB, fy: str | None = None, scope: str = "household", id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    profiles = await _profiles(db, principal, scope, id)
    _, inc = await _rows(db, principal, profiles, fy or tax.fy_of(date.today()))
    names = {p.id: p.display_name for p in profiles}
    return [{"id": str(i.id), "profile_id": str(i.profile_id), "profile_name": names.get(i.profile_id), "kind": i.kind, "symbol": i.symbol,
             "isin": i.isin, "ex_date": i.ex_date, "quantity": float(i.quantity), "per_unit": None if i.per_unit is None else float(i.per_unit),
             "amount": tax.money(i.amount), "broker": i.broker} for i in inc]


def _base(sym: str) -> str:
    return sym.upper().removesuffix(".NS").removesuffix(".BO").strip()


def _mark_out(m: HarvestMark, gains: list[RealisedGain]) -> dict[str, Any]:
    """reflected = an imported statement already contains this sale (then it isn't counted a second time)."""
    since = m.created_at.date() - timedelta(days=5)
    names = {_base(m.symbol), (m.name or "").upper().strip()}
    reflected = any(g.profile_id == m.profile_id and g.sell_date and g.sell_date >= since and _base(g.symbol) in names for g in gains)
    return {"id": str(m.id), "key": m.key, "kind": m.kind, "instrument_id": str(m.instrument_id) if m.instrument_id else None,
            "symbol": m.symbol, "name": m.name, "quantity": float(m.quantity), "booked": tax.money(m.booked), "st_part": tax.money(m.st_part),
            "lt_part": tax.money(m.lt_part), "tax_saved": tax.money(m.tax_saved), "done_at": m.created_at, "reflected": reflected}


class DoneIn(BaseModel):
    profile_id: uuid.UUID
    fy: str = Field(pattern=r"^\d{4}-\d{2}$")
    key: str = Field(max_length=80)
    kind: str = Field(pattern="^(loss|gain)$")
    instrument_id: uuid.UUID | None = None
    symbol: str = Field(max_length=160)
    name: str | None = Field(default=None, max_length=255)
    quantity: float = Field(ge=0)
    booked: float
    st_part: float = 0
    lt_part: float = 0
    tax_saved: float = Field(default=0, ge=0)


@router.post("/tax/harvest/done", status_code=201)
async def mark_done(body: DoneIn, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    """Record that a harvesting action was carried out, so the remaining ideas and savings update."""
    await access.get_profile(db, principal, body.profile_id, write=True)
    from decimal import Decimal

    m = HarvestMark(id=uuid.uuid4(), household_id=principal.household_id, profile_id=body.profile_id, fy=body.fy, key=body.key, kind=body.kind,
                    instrument_id=body.instrument_id, symbol=body.symbol, name=body.name, quantity=Decimal(str(body.quantity)),
                    booked=Decimal(str(round(body.booked, 2))), st_part=Decimal(str(round(body.st_part, 2))), lt_part=Decimal(str(round(body.lt_part, 2))),
                    tax_saved=Decimal(str(round(body.tax_saved, 2))))
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return _mark_out(m, [])


@router.delete("/tax/harvest/done/{mark_id}")
async def undo_done(mark_id: uuid.UUID, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    m = await db.get(HarvestMark, mark_id)
    if m is None or m.household_id != principal.household_id or m.deleted_at is not None:
        raise HTTPException(404, "Not found")
    await access.get_profile(db, principal, m.profile_id, write=True)
    from datetime import datetime

    m.deleted_at = datetime.now().astimezone()
    await db.commit()
    return {"id": str(m.id), "undone": True}


class CostIn(BaseModel):
    cost: float | None = Field(default=None, ge=0, le=1e11)
    bonus: bool = False  # "these are bonus shares": ₹0 cost is correct


@router.patch("/tax/realised/{gain_id}")
async def set_cost(gain_id: uuid.UUID, body: CostIn, db: DB, principal: Principal = require(P_TXN_WRITE)) -> dict[str, Any]:
    """Enter the real purchase cost for a sale the statement shows with a buy value of 0 (null = clear)."""
    g = await db.get(RealisedGain, gain_id)
    if g is None or g.household_id != principal.household_id or g.deleted_at is not None:
        raise HTTPException(404, "Sale not found")
    prof = await access.get_profile(db, principal, g.profile_id, write=True)
    from decimal import Decimal

    flags = [f for f in (g.flags or []) if not str(f).startswith(("cost_estimated", "bonus"))]
    if body.bonus:
        g.cost_override, g.flags = None, [f for f in flags if f != "zero_cost"] + ["bonus:marked by you"]
    else:
        g.cost_override = None if body.cost is None else Decimal(str(round(body.cost, 2)))
        g.flags = flags if "zero_cost" in flags else [*flags, "zero_cost"]
    await db.commit()
    return _gain_out(g, {prof.id: prof.display_name})
