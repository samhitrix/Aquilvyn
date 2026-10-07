"""Writing broker Tax P&L / capital-gains statements (realised lots + dividends) for one person.

A statement is the truth for its account and period: rows already stored for the same account
(or, without an account, the same person and broker) inside that period are replaced — kept and
marked ``superseded_by`` — so importing the Q1–Q2 file and later the full-year file never
doubles anything, and deleting an import brings back exactly what it replaced."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .importers.taxpnl import TaxRow
from .models import ImportJob, IncomeEvent, RealisedGain
from .tax import fy_of


def fy_bounds(fy: str) -> tuple[date, date]:
    y = int(fy[:4])
    return date(y, 4, 1), date(y + 1, 3, 31)


def period_of(rows: list[TaxRow], meta: dict[str, Any]) -> tuple[date, date] | None:
    if meta.get("period"):
        a, b = meta["period"]
        return date.fromisoformat(a), date.fromisoformat(b)
    ds = [r.sell_date for r in rows if r.sell_date]
    return (min(ds), max(ds)) if ds else None


def _scope(model: Any, profile_id: uuid.UUID, account_fp: str | None, broker: str | None) -> Any:
    if account_fp:
        return model.account_fp == account_fp
    return (model.profile_id == profile_id) & (model.account_fp.is_(None)) & (model.broker == broker if broker else model.broker.is_(None))


async def plan(db: AsyncSession, profile_id: uuid.UUID, rows: list[TaxRow], meta: dict[str, Any], account_fp: str | None) -> dict[str, Any]:
    """What the import would change — shown on the review screen before anything is written."""
    period = period_of(rows, meta)
    replaced_gains = replaced_income = 0
    if period:
        a, b = period
        replaced_gains = len((await db.execute(select(RealisedGain.id).where(
            _scope(RealisedGain, profile_id, account_fp, meta.get("broker")), RealisedGain.deleted_at.is_(None),
            RealisedGain.sell_date >= a, RealisedGain.sell_date <= b))).all())
        replaced_income = len((await db.execute(select(IncomeEvent.id).where(
            _scope(IncomeEvent, profile_id, account_fp, meta.get("broker")), IncomeEvent.deleted_at.is_(None),
            IncomeEvent.ex_date >= a, IncomeEvent.ex_date <= b))).all())
    gains = [r for r in rows if r.record == "gain"]
    income = [r for r in rows if r.record == "income"]
    return {"kind": "tax", "period": [d.isoformat() for d in period] if period else None,
            "counts": {"sales": len(gains), "dividends": sum(r.term == "dividend" for r in income), "interest": sum(r.term == "interest" for r in income),
                       "replaced_sales": replaced_gains, "replaced_income": replaced_income},
            "realised": float(sum((r.taxable_profit for r in gains), Decimal(0))), "income": float(sum((r.profit for r in income), Decimal(0)))}


async def write(db: AsyncSession, household_id: uuid.UUID, profile_id: uuid.UUID, job: ImportJob, rows: list[TaxRow],
                issues: list[dict[str, Any]], meta: dict[str, Any], account_fp: str | None) -> dict[str, Any]:
    broker = meta.get("broker")
    period = period_of(rows, meta)
    now = datetime.now().astimezone()
    replaced = 0
    if period:
        a, b = period
        for model, col in ((RealisedGain, RealisedGain.sell_date), (IncomeEvent, IncomeEvent.ex_date)):
            res = await db.execute(update(model).where(
                _scope(model, profile_id, account_fp, broker), model.deleted_at.is_(None), col >= a, col <= b,
            ).values(deleted_at=now, superseded_by=job.id))
            replaced += res.rowcount or 0  # type: ignore[attr-defined]
    fallback = period[1] if period else date.today()
    gains = income = 0
    for r in rows:
        when = r.sell_date or fallback
        common = {"id": uuid.uuid4(), "household_id": household_id, "profile_id": profile_id, "import_job_id": job.id, "account_fp": account_fp,
                  "broker": broker, "fy": fy_of(when), "symbol": r.symbol[:160], "isin": (r.isin or None) and r.isin[:12]}
        if r.record == "gain":
            db.add(RealisedGain(**common, asset=r.asset, term=r.term, buy_date=r.buy_date, sell_date=r.sell_date, quantity=r.quantity,
                                buy_value=r.buy_value, sell_value=r.sell_value, profit=r.profit, taxable_profit=r.taxable_profit,
                                days_held=r.days_held, section=r.section[:160], flags=list(r.flags)))
            gains += 1
        else:
            db.add(IncomeEvent(**common, kind=r.term, ex_date=r.sell_date, quantity=r.quantity, per_unit=r.per_unit, amount=r.profit))
            income += 1
    await db.flush()
    await refresh_statuses(db, household_id)
    warnings = [e for e in issues if e.get("level") == "warning"]
    job.status = "done"
    job.stats = {"created": gains + income, "sales": gains, "income": income, "replaced": replaced, "duplicates_skipped": 0,
                 "errors": 0, "warnings": len(warnings), "broker": broker, "period": [d.isoformat() for d in period] if period else None,
                 "checks": meta.get("checks") or [], "charges": meta.get("charges"), "portfolios": ["Tax statements"]}
    job.errors = warnings[:50]
    await db.commit()
    return {"id": str(job.id), "status": "done", "kind": job.kind, "stats": job.stats, "errors": job.errors}


async def refresh_statuses(db: AsyncSession, household_id: uuid.UUID) -> None:
    """A tax import whose rows were all replaced by a later statement shows as 'replaced'."""
    jobs = (await db.execute(select(ImportJob).where(ImportJob.household_id == household_id, ImportJob.kind == "tax_pnl",
                                                     ImportJob.status.in_(["done", "replaced"])))).scalars().all()
    for j in jobs:
        live = await db.scalar(select(RealisedGain.id).where(RealisedGain.import_job_id == j.id, RealisedGain.deleted_at.is_(None)).limit(1)) \
            or await db.scalar(select(IncomeEvent.id).where(IncomeEvent.import_job_id == j.id, IncomeEvent.deleted_at.is_(None)).limit(1))
        j.status = "done" if live else "replaced"


async def undo(db: AsyncSession, job: ImportJob) -> dict[str, int]:
    """Delete a tax import: its rows go, and whatever it replaced comes back."""
    now = datetime.now().astimezone()
    removed = restored = 0
    for model in (RealisedGain, IncomeEvent):
        res = await db.execute(update(model).where(model.import_job_id == job.id, model.deleted_at.is_(None)).values(deleted_at=now))
        removed += res.rowcount or 0  # type: ignore[attr-defined]
        res = await db.execute(update(model).where(model.superseded_by == job.id).values(deleted_at=None, superseded_by=None))
        restored += res.rowcount or 0  # type: ignore[attr-defined]
    await db.flush()
    return {"deleted": removed, "restored": restored}


async def restore(db: AsyncSession, job: ImportJob) -> dict[str, int]:
    """Bring a replaced tax import back; the rows that replaced it (same account, same dates) step aside."""
    now = datetime.now().astimezone()
    back = superseded = 0
    for model, col in ((RealisedGain, RealisedGain.sell_date), (IncomeEvent, IncomeEvent.ex_date)):
        rows = (await db.execute(select(model).where(model.import_job_id == job.id, model.deleted_at.is_not(None)))).scalars().all()
        if not rows:
            continue
        ds = [getattr(r, col.key) for r in rows if getattr(r, col.key)]
        if ds:
            r0 = rows[0]
            res = await db.execute(update(model).where(
                _scope(model, r0.profile_id, r0.account_fp, r0.broker), model.deleted_at.is_(None), model.import_job_id != job.id,
                or_(col.is_(None), (col >= min(ds)) & (col <= max(ds))),
            ).values(deleted_at=now, superseded_by=job.id))
            superseded += res.rowcount or 0  # type: ignore[attr-defined]
        for r in rows:
            r.deleted_at, r.superseded_by = None, None
            back += 1
    await db.flush()
    await refresh_statuses(db, job.household_id)
    return {"restored": back, "superseded": superseded}
