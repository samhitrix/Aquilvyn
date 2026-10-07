"""History, fundamentals, news, corporate actions — DB-first with provider refresh, cached."""
from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.cache import cache
from fm_common.events import publish
from fm_common.logging import get_logger
from fm_common.redact import redact

from . import health
from .config import settings
from .models import CorporateAction, CorporateActionType, Fundamentals, Instrument, PriceEOD
from .providers import get_provider

log = get_logger(__name__)


async def history(db: AsyncSession, inst: Instrument, days: int = 365, force: bool = False) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        since = date.today() - timedelta(days=days)
        rows = (
            await db.execute(
                select(PriceEOD).where(PriceEOD.instrument_id == inst.id, PriceEOD.price_date >= since).order_by(PriceEOD.price_date)
            )
        ).scalars().all()
        if getattr(get_provider(), "live", False) and any(_simulated(r.source) for r in rows):
            # history cached while running in simulated mode must never stand in for real prices
            # (it made the market regime read "bull" from a simulated Nifty)
            await db.execute(delete(PriceEOD).where(PriceEOD.instrument_id == inst.id, PriceEOD.source.like("simulated%")))
            await db.commit()
            rows = [r for r in rows if not _simulated(r.source)]
        fresh = not force and rows and rows[-1].price_date >= date.today() - timedelta(days=4) and len(rows) >= days * 0.6
        if not fresh and not inst.asset_type.is_accrual:
            try:
                bars = await get_provider().history(inst.symbol, max(days, 400))
                await upsert_bars(db, inst.id, [b.to_dict() for b in bars], source=get_provider().name)
                rows = (
                    await db.execute(
                        select(PriceEOD).where(PriceEOD.instrument_id == inst.id, PriceEOD.price_date >= since).order_by(PriceEOD.price_date)
                    )
                ).scalars().all()
            except Exception as exc:
                log.warning("market.history_refresh_failed", symbol=inst.symbol, error=redact(exc)[:300])
        return [
            {"date": r.price_date.isoformat(), "open": _f(r.open), "high": _f(r.high), "low": _f(r.low),
             "close": float(r.close), "volume": r.volume or 0, "source": r.source}
            for r in rows
        ]

    if force:
        await cache.invalidate(prefix=f"hist:{inst.id}:")
    return await cache.get_or_load(f"hist:{inst.id}:{days}", load, ttl=settings.history_cache_ttl_seconds, l1_ttl=60)


def _simulated(source: str | None) -> bool:
    return (source or "").startswith("simulated")


def _f(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


async def upsert_bars(db: AsyncSession, instrument_id: uuid.UUID, bars: list[dict[str, Any]], source: str) -> int:
    if not bars:
        return 0
    values = [
        {"instrument_id": instrument_id, "price_date": date.fromisoformat(b["date"]) if isinstance(b["date"], str) else b["date"],
         "open": b.get("open"), "high": b.get("high"), "low": b.get("low"), "close": b["close"], "volume": b.get("volume"), "source": source}
        for b in bars
    ]
    total = 0
    for i in range(0, len(values), 1000):
        stmt = insert(PriceEOD).values(values[i : i + 1000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "price_date"],
            set_={k: getattr(stmt.excluded, k) for k in ("open", "high", "low", "close", "volume", "source")},
            where=PriceEOD.source != "manual",  # never overwrite a user-entered NAV
        )
        total += (await db.execute(stmt)).rowcount or 0
    await db.commit()
    await cache.invalidate(prefix=f"hist:{instrument_id}:")
    return total


# NSE series that are not company shares: gold bonds (GB), G-secs (GS), T-bills (TB), bonds / NCDs (N*, Y*, Z*)
NOT_A_COMPANY = re.compile(r"-(GB|GS|TB|SG|N[0-9A-Z]|Y[0-9A-Z]|Z[0-9A-Z])\.(NS|BO)$", re.I)


def has_fundamentals(inst: Instrument) -> bool:
    return inst.asset_type.value in ("stock", "etf", "mutual_fund", "reit") and not NOT_A_COMPANY.search(inst.symbol or "")


async def fundamentals(db: AsyncSession, inst: Instrument, force: bool = False) -> dict[str, Any] | None:
    async def load() -> dict[str, Any] | None:
        row = await db.get(Fundamentals, inst.id)
        stale = (row is None or row.as_of < datetime.now(UTC) - timedelta(seconds=settings.fundamentals_cache_ttl_seconds)
                 or (getattr(get_provider(), "live", False) and _simulated(row.source)))
        if (stale or force) and has_fundamentals(inst):
            try:
                data = await get_provider().fundamentals(inst.symbol)
            except Exception as exc:  # recorded, so Settings → Data sources and the advisor can say *why* it's missing
                data = None
                log.warning("market.fundamentals_failed", symbol=inst.symbol, error=redact(exc)[:300])
                if inst.asset_type.value == "stock":
                    await health.record("fundamentals", ok=False, error=f"{inst.symbol}: {redact(exc)[:300] or type(exc).__name__}")
            else:
                if inst.asset_type.value == "stock":
                    if data:
                        await health.record("fundamentals", ok=True, count=1, note=f"last used: {'+'.join(data.get('_sources') or ['?'])}")
                    else:
                        await health.record("fundamentals", ok=False, error=f"{inst.symbol}: the source returned no company data")
            if data:
                used = data.pop("_sources", None)
                src = f"{get_provider().name}:{'+'.join(used)}" if used else get_provider().name
                stmt = insert(Fundamentals).values(instrument_id=inst.id, data=data, source=src, as_of=datetime.now(UTC))
                stmt = stmt.on_conflict_do_update(index_elements=["instrument_id"], set_={"data": stmt.excluded.data, "source": stmt.excluded.source, "as_of": stmt.excluded.as_of})
                await db.execute(stmt)
                await db.commit()
                await publish("fundamentals.updated", {"instrument_id": str(inst.id), "symbol": inst.symbol})
                row = await db.get(Fundamentals, inst.id)
        if row is None:
            return None
        return {"data": row.data, "source": row.source, "as_of": row.as_of.isoformat()}

    key = f"fund:{inst.id}"
    if force:
        await cache.invalidate(key, f"{key}:miss")
    elif await cache.get(f"{key}:miss") is True:
        return None  # every source failed a few minutes ago — don't hammer them on each page view
    res = await cache.get_or_load(key, load, ttl=3600, l1_ttl=300)
    if res is None:  # a miss is retried after 10 minutes, not remembered for an hour
        await cache.invalidate(key)
        await cache.set(f"{key}:miss", True, ttl=600, l1_ttl=60)
    return res


async def news(inst: Instrument, limit: int = 10) -> list[dict[str, Any]]:
    return await cache.get_or_load(f"news:{inst.symbol}", lambda: get_provider().news(inst.symbol, limit), ttl=900, l1_ttl=120)


async def sync_corporate_actions(db: AsyncSession, inst: Instrument) -> int:
    """E35: pull splits/bonuses/dividends and store them; portfolio-svc applies them to ledgers."""
    acts = await get_provider().corporate_actions(inst.symbol)
    new = 0
    for a in acts:
        stmt = insert(CorporateAction).values(
            id=uuid.uuid4(), instrument_id=inst.id, action_type=CorporateActionType(a["action_type"]),
            ex_date=date.fromisoformat(a["ex_date"]), ratio_from=a.get("ratio_from"), ratio_to=a.get("ratio_to"),
            amount=a.get("amount"), source=get_provider().name,
        ).on_conflict_do_nothing(constraint="uq_corp_action").returning(CorporateAction.id)
        created = (await db.execute(stmt)).scalar_one_or_none()
        if created:
            new += 1
            await publish("corporate_action.detected", {"instrument_id": str(inst.id), "symbol": inst.symbol, **a})
    await db.commit()
    return new


async def corporate_actions(db: AsyncSession, instrument_ids: list[uuid.UUID], since: date | None = None) -> list[dict[str, Any]]:
    stmt = select(CorporateAction).where(CorporateAction.instrument_id.in_(instrument_ids))
    if since:
        stmt = stmt.where(CorporateAction.ex_date >= since)
    rows = (await db.execute(stmt.order_by(CorporateAction.ex_date))).scalars()
    return [
        {"id": str(r.id), "instrument_id": str(r.instrument_id), "action_type": r.action_type.value, "ex_date": r.ex_date.isoformat(),
         "ratio_from": _f(r.ratio_from), "ratio_to": _f(r.ratio_to), "amount": _f(r.amount), "source": r.source}
        for r in rows
    ]
