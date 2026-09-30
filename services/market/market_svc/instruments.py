"""Security master operations + idempotent seeding of the universe."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.logging import get_logger

from .config import settings
from .models import AssetType, Instrument
from .providers import get_provider
from .providers.universe import INDICES, MUTUAL_FUNDS, NPS_SCHEMES, SECTOR_INDEX, STOCKS

log = get_logger(__name__)


def instrument_dict(i: Instrument) -> dict[str, Any]:
    return {
        "id": str(i.id), "symbol": i.symbol, "name": i.name, "asset_type": i.asset_type.value,
        "exchange": i.exchange, "currency": i.currency, "isin": i.isin, "sector": i.sector,
        "industry": i.industry, "benchmark_symbol": i.benchmark_symbol, "meta": i.meta,
        "household_id": str(i.household_id) if i.household_id else None,
    }


def _seed_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sym, name, _ in INDICES:
        rows.append({"symbol": sym, "name": name, "asset_type": AssetType.INDEX, "exchange": "NSE" if "BSESN" not in sym else "BSE", "meta": {}})
    for sym, name, sector, *_ in STOCKS:
        kind = AssetType.ETF if sector == "ETF" else AssetType.STOCK
        rows.append({
            "symbol": sym, "name": name, "asset_type": kind, "exchange": "NSE", "sector": None if kind == AssetType.ETF else sector,
            "benchmark_symbol": SECTOR_INDEX.get(sector, "^NSEI") if kind == AssetType.STOCK else "^NSEI", "meta": {},
        })
    for code, name, cat, plan, er, *_ in MUTUAL_FUNDS:
        bench = "^NSEI" if cat.startswith("equity:large") or cat.endswith("index") else "^CRSLDX" if cat.startswith("equity") else None
        rows.append({
            "symbol": code, "name": name, "asset_type": AssetType.MUTUAL_FUND, "exchange": "AMFI", "benchmark_symbol": bench,
            "meta": {"mf_category": cat, "plan": plan, "expense_ratio": er, "amc": name.split(" ")[0]},
        })
    for sym, name, cls, *_ in NPS_SCHEMES:
        rows.append({
            "symbol": sym, "name": name, "asset_type": AssetType.NPS, "exchange": "PFRDA",
            "benchmark_symbol": "^NSEI" if cls == "E" else None, "meta": {"pfm": sym.split("-")[1], "tier": "I", "scheme": cls},
        })
    return rows


SEED_COLUMNS = ("symbol", "name", "asset_type", "exchange", "sector", "benchmark_symbol", "meta")


async def seed_universe(db: AsyncSession) -> int:
    """Idempotent upsert of the seed universe. Every row carries the same keys — a multi-row
    INSERT takes its column list from the first row, so missing keys would silently drop data."""
    rows = [{"id": uuid.uuid4(), **{c: r.get(c) for c in SEED_COLUMNS}} for r in _seed_rows()]
    for r in rows:
        r["meta"] = r["meta"] or {}
    stmt = insert(Instrument).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_instruments_symbol_type_household",
        set_={c: getattr(stmt.excluded, c) for c in ("name", "exchange", "sector", "benchmark_symbol", "meta")},
    )
    res = await db.execute(stmt)
    await db.commit()
    return res.rowcount or 0


async def search(db: AsyncSession, household_id: uuid.UUID, q: str, limit: int = 15) -> list[dict[str, Any]]:
    like = f"%{q.lower()}%"
    stmt = (
        select(Instrument)
        .where(
            or_(Instrument.household_id.is_(None), Instrument.household_id == household_id),
            Instrument.is_active.is_(True),
            or_(func.lower(Instrument.symbol).like(like), func.lower(Instrument.name).like(like), Instrument.isin == q.upper()),
        )
        .order_by(func.length(Instrument.symbol))
        .limit(limit)
    )
    found = [instrument_dict(i) for i in (await db.execute(stmt)).scalars()]
    if len(found) < 5:
        try:
            for hit in await get_provider().search(q, limit):
                if not any(f["symbol"] == hit["symbol"] for f in found):
                    found.append({**hit, "id": None, "unregistered": True})
        except Exception as exc:
            log.warning("instruments.provider_search_failed", error=str(exc))
    return found[:limit]


async def get_by_symbol(db: AsyncSession, symbol: str, household_id: uuid.UUID | None = None) -> Instrument | None:
    stmt = select(Instrument).where(
        Instrument.symbol == symbol, or_(Instrument.household_id.is_(None), Instrument.household_id == household_id)
    )
    return (await db.execute(stmt)).scalars().first()


_REPAIR_TRIED: dict[str, float] = {}


def clean_scheme_name(raw: str | None) -> str | None:
    """'127FMGDG-Motilal Oswal Midcap Fund - Direct Plan Growth (Demat)' → 'Motilal Oswal Midcap
    Fund - Direct Plan Growth' (drops the RTA scheme code and holding-mode suffixes)."""
    if not raw:
        return None
    n = re.sub(r"^\s*[A-Z0-9]{2,12}-(?=[A-Za-z])", "", raw.strip())
    n = re.sub(r"\((non[- ]?)?demat\)|\(formerly[^)]*\)", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" -")
    return n or None


def _best_name(provider_name: str | None, hint: str | None) -> str | None:
    """Prefer the statement's name when it states the plan and the provider's doesn't."""
    from .providers.mfapi import plan_from_name

    hint = clean_scheme_name(hint)
    if provider_name and (plan_from_name(provider_name) or not plan_from_name(hint)):
        return provider_name
    return hint or provider_name


def _mf_meta_from_name(name: str) -> dict[str, Any]:
    from .providers.mfapi import _category, plan_from_name

    plan = plan_from_name(name)
    return {"mf_category": _category(name), **({"plan": plan} if plan else {})}


def fix_mf_plan(inst: Instrument) -> bool:
    """Earlier versions defaulted plan to 'regular' when the name didn't say 'direct'."""
    from .providers.mfapi import plan_from_name

    if inst.asset_type != AssetType.MUTUAL_FUND or not inst.meta:
        return False
    want = plan_from_name(inst.name)
    if inst.meta.get("plan") != want:
        meta = {k: v for k, v in inst.meta.items() if k != "plan"}
        inst.meta = {**meta, **({"plan": want} if want else {})}
        return True
    return False


def _needs_mf_repair(inst: Instrument) -> bool:
    """Funds registered while the NAV source was unavailable / simulated got the AMFI code as their
    name and a stock-like 'Diversified' sector."""
    return inst.asset_type == AssetType.MUTUAL_FUND and inst.household_id is None and (inst.name == inst.symbol or inst.sector == "Diversified")


async def _apply_mf_details(inst: Instrument, fund: dict[str, Any], name_hint: str | None) -> None:
    name = _best_name(fund.get("name"), name_hint)
    if name and name != inst.symbol:
        inst.name = name[:200]
        base = _mf_meta_from_name(name)
        inst.meta = {**{k: v for k, v in (inst.meta or {}).items() if k != "plan"}, **base,
                     **{k: fund.get(k) for k in ("mf_category", "plan", "amc") if fund.get(k)}}
    inst.sector = None
    inst.isin = inst.isin or fund.get("isin")


async def register(db: AsyncSession, symbol: str, asset_type: AssetType | None = None, name: str | None = None, isin: str | None = None,
                   sector: str | None = None) -> Instrument:
    """Register a public instrument discovered via provider search (idempotent). ``name``/``isin``
    are hints from the source document (e.g. the scheme name printed on a CAS)."""
    existing = await get_by_symbol(db, symbol)
    if existing:
        if sector and existing.sector != sector and existing.asset_type == AssetType.STOCK and existing.household_id is None:
            existing.sector = sector  # the broker statement's classification wins
            await db.commit()
        if _needs_mf_repair(existing):
            await repair_mf(db, [existing], {symbol: name} if name else None)
        elif existing.asset_type == AssetType.MUTUAL_FUND and existing.household_id is None and name:
            better = _best_name(existing.name, name)
            if better and better != existing.name:  # statement names the plan; stored name didn't
                existing.name = better[:200]
                fix_mf_plan(existing)
                await db.commit()
        return existing
    provider = get_provider()
    try:
        fund = await provider.fundamentals(symbol) or {}
    except Exception as exc:
        log.warning("instrument.fundamentals_failed", symbol=symbol, error=str(exc))
        fund = {}
    kind = asset_type or (AssetType.MUTUAL_FUND if symbol.isdigit() else AssetType.INDEX if symbol.startswith("^") else AssetType.STOCK)
    meta: dict[str, Any] = {}
    if kind == AssetType.MUTUAL_FUND:
        best = _best_name(fund.get("name"), name)
        fund = {**fund, "name": best, "plan": None}
        meta = {**(_mf_meta_from_name(best) if best else {}), **{k: fund.get(k) for k in ("mf_category", "amc") if fund.get(k)}}
    sector = (sector or fund.get("sector")) if kind == AssetType.STOCK else fund.get("sector") if kind != AssetType.MUTUAL_FUND else None
    if kind == AssetType.NPS:  # 'NPS-ICICI-E-T1' → scheme class E (equity) / C / G (debt) / A
        m = re.match(r"NPS-[A-Z]+-([ECGA])-", symbol)
        meta = {"scheme": m.group(1) if m else "E"}
    inst = Instrument(
        id=uuid.uuid4(), symbol=symbol, name=(fund.get("name") or name or symbol)[:200], asset_type=kind,
        exchange="AMFI" if kind == AssetType.MUTUAL_FUND else "PFRDA" if kind == AssetType.NPS else ("BSE" if symbol.endswith(".BO") else "NSE"),
        sector=sector, industry=fund.get("industry"), isin=fund.get("isin") or isin,
        benchmark_symbol=SECTOR_INDEX.get(sector or "", "^NSEI") if kind == AssetType.STOCK else ("^CRSLDX" if kind == AssetType.MUTUAL_FUND else None),
        meta=meta,
    )
    db.add(inst)
    await db.commit()
    return inst


async def repair_mf(db: AsyncSession, insts: list[Instrument], hints: dict[str, str | None] | None = None) -> None:
    """Best-effort: fill real scheme names/categories for funds stored with just their code."""
    if sum(fix_mf_plan(i) for i in insts if i.household_id is None):
        await db.commit()
    now = time.monotonic()
    todo = [i for i in insts if _needs_mf_repair(i) and now - _REPAIR_TRIED.get(i.symbol, -1e9) > 3600][:40]
    if not todo:
        return
    for i in todo:
        _REPAIR_TRIED[i.symbol] = now  # at most one attempt per fund per hour (offline-safe)
    provider = get_provider()

    async def one(i: Instrument) -> None:
        try:
            fund = await asyncio.wait_for(provider.fundamentals(i.symbol), 8) or {}
        except Exception:
            fund = {}
        await _apply_mf_details(i, fund, (hints or {}).get(i.symbol))

    await asyncio.gather(*(one(i) for i in todo))
    await db.commit()


async def by_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[Instrument]:
    if not ids:
        return []
    return list((await db.execute(select(Instrument).where(Instrument.id.in_(ids)))).scalars())


_WORDS = re.compile(r"[a-z0-9]+")
_NOISE = {"fund", "plan", "option", "the", "of", "and", "mutual", "scheme"}


def _tokens(name: str) -> set[str]:
    return {w for w in _WORDS.findall(name.lower()) if w not in _NOISE}


async def search_fund_code(name: str) -> dict[str, str] | None:
    """Best AMFI-code match for a scheme name (used when a statement has no ISIN). Requires the
    plan (direct/regular) and option (growth/IDCW) to agree, then the most shared words."""
    try:
        results = await get_provider().mf.search(name, 20)
    except Exception:
        return None
    want = _tokens(name)
    direct = "direct" in want
    growth = "growth" in want or not ({"idcw", "dividend"} & want)
    best, score = None, 0.0
    for r in results:
        cand = _tokens(r["name"])
        if ("direct" in cand) != direct or (("growth" in cand) != growth and ("growth" in want or {"idcw", "dividend"} & cand)):
            continue
        sc = len(want & cand) / max(1, len(want | cand))
        if sc > score:
            best, score = r, sc
    return {"code": best["symbol"], "name": best["name"]} if best and score >= 0.5 else None


def cap_bucket(market_cap: float | None) -> str | None:
    """Large / mid / small by market cap (₹). SEBI defines them by rank (top 100 / 101–250 / rest);
    AMFI publishes the cut-offs twice a year — the configured values approximate the latest list."""
    if not market_cap:
        return None
    if market_cap >= settings.large_cap_min_inr:
        return "large"
    if market_cap >= settings.mid_cap_min_inr:
        return "mid"
    return "small"


_BACKFILLING: set[uuid.UUID] = set()


async def _backfill(ids: list[uuid.UUID]) -> None:
    from fm_common.db.session import SessionLocal

    from . import data

    try:
        async with SessionLocal() as db:
            for iid in ids:
                inst = await db.get(Instrument, iid)
                if inst is not None:
                    try:
                        await asyncio.wait_for(data.fundamentals(db, inst), 20)
                    except Exception as exc:
                        log.warning("classify.fundamentals_failed", symbol=inst.symbol, error=str(exc) or type(exc).__name__)
    finally:
        _BACKFILLING.difference_update(ids)


async def classify(db: AsyncSession, ids: list[uuid.UUID]) -> dict[str, Any]:
    from .models import Fundamentals

    insts = await by_ids(db, ids)
    funds = {r.instrument_id: r.data for r in (await db.execute(select(Fundamentals).where(Fundamentals.instrument_id.in_([i.id for i in insts])))).scalars()}
    from .providers import nse_index

    try:
        lists = await asyncio.wait_for(nse_index.size_lists(), 15)
    except Exception:
        lists = {}
    out: dict[str, Any] = {}
    need_fundamentals: list[uuid.UUID] = []
    for i in insts:
        f = funds.get(i.id) or {}
        mcap = f.get("market_cap")
        cap, source = None, None
        if i.asset_type == AssetType.STOCK:
            # 1) NSE Nifty 100 / Midcap 150 membership (SEBI's top-100 / 101–250 bands), 2) market cap
            cap = nse_index.bucket(lists, i.symbol, i.isin) if i.exchange != "BSE" else None
            source = "nse_index" if cap else None
            if cap is None and mcap:
                cap, source = cap_bucket(mcap), "market_cap"
            if (cap is None or not i.sector) and i.id not in funds and i.id not in _BACKFILLING:
                need_fundamentals.append(i.id)
        sector = i.sector or f.get("sector") or (nse_index.industry(lists, i.symbol, i.isin) if i.asset_type == AssetType.STOCK else None)
        out[str(i.id)] = {"sector": sector, "market_cap": mcap, "cap": cap, "cap_source": source,
                          "pending": i.id in _BACKFILLING or i.id in need_fundamentals}
    if need_fundamentals:
        _BACKFILLING.update(need_fundamentals[:40])
        asyncio.create_task(_backfill(need_fundamentals[:40]))
    return out
