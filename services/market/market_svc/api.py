from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from fm_common.cache import cache
from fm_common.db.session import SessionLocal
from fm_common.deps import DB, CurrentUser, get_ws_principal
from fm_common.logging import get_logger
from fm_common.redact import redact

from . import data, health, instruments, quality
from .models import AssetType, Instrument
from .providers import get_provider
from .providers.universe import INDICES, SECTOR_INDEX, STOCKS
from .quotes import get_quotes, store_reference_quotes
from .stream import hub

router = APIRouter(prefix="/market", tags=["market"])
ws_router = APIRouter(tags=["market-stream"])
log = get_logger(__name__)


async def _inst(db: DB, symbol: str, principal: CurrentUser) -> Instrument:
    inst = await instruments.get_by_symbol(db, symbol, principal.household_id)
    if inst is None:
        try:
            inst = await instruments.register(db, symbol)
        except Exception as exc:
            raise HTTPException(404, f"Unknown instrument {symbol}") from exc
    return inst


class FundsIn(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    force: bool = False


@router.get("/funds/peers/{code}")
async def fund_peers(code: str, principal: CurrentUser) -> dict[str, Any]:
    """Same-category alternatives for a mutual fund (AMFI category; Direct · Growth; other plans of it excluded)."""
    from . import fund_holdings
    from .providers import amfi

    return fund_holdings.fund_peers(code, await amfi.by_code())


@router.post("/funds/lookthrough")
async def funds_lookthrough(body: FundsIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """What's inside each fund / ETF (from its fund house's latest monthly portfolio) — MF look-through."""
    from . import fund_holdings

    return await fund_holdings.lookthrough(db, body.ids)


@router.post("/funds/refresh")
async def funds_refresh(body: FundsIn, principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    """Fetch fund houses' monthly portfolios now (only what's due unless ``force``); one report line per fund house."""
    from . import fund_holdings

    codes = None
    if body.ids:
        insts = (await db.execute(select(Instrument).where(Instrument.id.in_(body.ids)))).scalars().all()
        codes = set((await fund_holdings.fund_codes(db, list(insts))).values())
        if not codes:
            return []
    return await fund_holdings.refresh(db, codes=codes, force=body.force)


@router.get("/instruments/search")
async def search(q: str, principal: CurrentUser, db: DB, limit: int = 15) -> list[dict[str, Any]]:
    return await instruments.search(db, principal.household_id, q, limit)


class RegisterIn(BaseModel):
    symbol: str
    asset_type: AssetType | None = None
    name: str | None = Field(default=None, max_length=200)  # hint from the source document (CAS scheme name)
    isin: str | None = Field(default=None, max_length=12)
    sector: str | None = Field(default=None, max_length=80)  # hint from the statement (used when the provider has none)


@router.post("/instruments/register")
async def register(body: RegisterIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    return instruments.instrument_dict(await instruments.register(db, body.symbol, body.asset_type, body.name, body.isin, body.sector))


class PrivateInstrumentIn(BaseModel):
    name: str = Field(max_length=200)
    asset_type: AssetType
    symbol: str | None = None
    meta: dict[str, Any] = {}


@router.post("/instruments/private", status_code=201)
async def create_private(body: PrivateInstrumentIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """Household-private instruments: EPF/VPF/PPF accounts, FDs, bonds, NPS (manual NAV)."""
    if not (body.asset_type.is_accrual or body.asset_type in (AssetType.NPS, AssetType.GOLD)):
        raise HTTPException(400, "Market-traded instruments must be registered, not created privately")
    inst = Instrument(
        id=uuid.uuid4(), household_id=principal.household_id,
        symbol=body.symbol or f"{body.asset_type.value.upper()}-{uuid.uuid4().hex[:8]}",
        name=body.name, asset_type=body.asset_type, exchange={"epf": "EPFO", "vpf": "EPFO", "nps": "PFRDA"}.get(body.asset_type.value),
        meta=body.meta,
    )
    db.add(inst)
    await db.commit()
    return instruments.instrument_dict(inst)


class BatchIn(BaseModel):
    ids: list[uuid.UUID]


@router.post("/instruments/batch")
async def batch(body: BatchIn, principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = await instruments.by_ids(db, body.ids)
    await instruments.repair_mf(db, rows)
    return [instruments.instrument_dict(i) for i in rows if i.household_id in (None, principal.household_id)]


class ClassifyIn(BaseModel):
    ids: list[uuid.UUID] = Field(max_length=1000)


class IsinIn(BaseModel):
    isins: list[str] = Field(max_length=1000)


@router.post("/instruments/by-isin")
async def by_isin(body: IsinIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """ISIN → trading symbol, for broker files that print company names or their own codes (ICICI "RELIND")."""
    from .providers import isin as isin_mod

    wanted = [i.strip().upper() for i in body.isins if i]
    known = {i.isin: {"symbol": i.symbol.removesuffix(".NS").removesuffix(".BO"), "exchange": i.exchange or "NSE", "name": i.name or i.symbol}
             for i in (await db.execute(select(Instrument).where(Instrument.isin.in_(wanted), Instrument.household_id.is_(None)))).scalars()
             if i.isin and not i.symbol.isdigit()}
    return await isin_mod.lookup(wanted, known)


@router.post("/instruments/classify")
async def classify(body: ClassifyIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """Sector + market cap + SEBI/AMFI-style size bucket per instrument (cached fundamentals only —
    missing ones are fetched in the background, so the next call is complete)."""
    return await instruments.classify(db, body.ids)


@router.get("/quotes")
async def quotes(principal: CurrentUser, symbols: str = Query(..., description="comma separated"),
                 wait: float = Query(4.0, ge=0, le=25, description="seconds to wait for sources before falling back")) -> dict[str, Any]:
    return await get_quotes([s.strip() for s in symbols.split(",") if s.strip()][:200], wait=wait)


@router.get("/status")
async def status(principal: CurrentUser) -> dict[str, Any]:
    """Where prices come from and whether each source is working right now."""
    from .config import settings as mkt

    return {"mode": get_provider().name, "simulated_equities": mkt.market_data_provider == "simulated", "sources": await health.snapshot()}


class ReferenceIn(BaseModel):
    prices: dict[str, dict[str, Any]] = Field(max_length=2000)


@router.post("/quotes/reference")
async def reference_quotes(body: ReferenceIn, principal: CurrentUser) -> dict[str, int]:
    return {"stored": await store_reference_quotes(body.prices)}


class FundRef(BaseModel):
    key: str
    isin: str | None = None
    name: str | None = None


class FundResolveIn(BaseModel):
    items: list[FundRef] = Field(max_length=500)


@router.post("/mf/resolve")
async def resolve_funds(body: FundResolveIn, principal: CurrentUser) -> dict[str, Any]:
    """ISIN (preferred, via the AMFI scheme master) or scheme name → AMFI code."""
    from .providers import amfi

    found = await amfi.by_isin([i.isin for i in body.items if i.isin])
    out: dict[str, Any] = {}
    for it in body.items:
        hit = found.get(it.isin or "")
        if hit:
            out[it.key] = {"code": hit["code"], "name": hit["name"], "via": "isin"}
        elif it.name:
            code = await instruments.search_fund_code(it.name)
            if code:
                out[it.key] = {**code, "via": "name"}
    return out


@router.get("/history/{symbol}")
async def history(symbol: str, principal: CurrentUser, db: DB, days: int = Query(365, le=3650), refresh: bool = False) -> dict[str, Any]:
    inst = await _inst(db, symbol, principal)
    return {"instrument": instruments.instrument_dict(inst), "bars": await data.history(db, inst, days, force=refresh)}


FUND_READY_FIELDS = ("pe", "pb", "roe", "roce", "debt_to_equity", "profit_margin", "operating_margin", "revenue_growth",
                     "earnings_growth", "market_cap", "eps", "book_value")


class CoverageIn(BaseModel):
    symbols: list[str] = Field(max_length=500)


@router.post("/coverage")
async def coverage(body: CoverageIn, principal: CurrentUser, db: DB) -> dict[str, Any]:
    """What market data is stored for each symbol right now — read from the database only (no provider calls),
    so the readiness engine can check hundreds of holdings in one cheap request."""
    from sqlalchemy import func as sa_func

    from .models import Fundamentals, PriceEOD

    insts = (await db.execute(select(Instrument).where(
        Instrument.symbol.in_(body.symbols), or_(Instrument.household_id.is_(None), Instrument.household_id == principal.household_id)))).scalars().all()
    by_id = {i.id: i for i in insts}
    if not by_id:
        return {"as_of": datetime.now(UTC).isoformat(), "instruments": {}}
    since = date.today() - timedelta(days=400)
    bars = {iid: (n, last) for iid, n, last in (await db.execute(
        select(PriceEOD.instrument_id, sa_func.count(), sa_func.max(PriceEOD.price_date))
        .where(PriceEOD.instrument_id.in_(list(by_id)), PriceEOD.price_date >= since).group_by(PriceEOD.instrument_id))).all()}
    funds = {f.instrument_id: f for f in (await db.execute(select(Fundamentals).where(Fundamentals.instrument_id.in_(list(by_id))))).scalars()}
    out: dict[str, Any] = {}
    for iid, inst in by_id.items():
        n, last = bars.get(iid, (0, None))
        f = funds.get(iid)
        fields = [k for k in FUND_READY_FIELDS if f and (f.data or {}).get(k) is not None]
        out[inst.symbol] = {
            "instrument_id": str(iid), "asset_type": inst.asset_type.value,
            "bars_400d": int(n), "last_bar": last.isoformat() if last else None,
            "fundamentals": {"as_of": f.as_of.isoformat(), "source": f.source, "fields": fields} if f else None,
        }
    return {"as_of": datetime.now(UTC).isoformat(), "instruments": out}


@router.get("/fundamentals/{symbol}")
async def fundamentals(symbol: str, principal: CurrentUser, db: DB, refresh: bool = False) -> dict[str, Any]:
    inst = await _inst(db, symbol, principal)
    return {"instrument": instruments.instrument_dict(inst), "fundamentals": await data.fundamentals(db, inst, force=refresh)}


async def _probe(group: str, name: str, fn: Any, summarize: Any, limit_s: float = 20) -> dict[str, Any]:
    """Run one real request against one source and describe the outcome in words (ok / empty / error)."""
    import time as _t

    t0 = _t.perf_counter()
    try:
        res = await asyncio.wait_for(fn(), limit_s)
        status, detail = summarize(res)
        return {"group": group, "source": name, "status": status, "detail": detail, "ms": int((_t.perf_counter() - t0) * 1000)}
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        if isinstance(exc, TimeoutError):
            msg = f"no answer within {limit_s:.0f}s"
        return {"group": group, "source": name, "status": "error", "detail": redact(msg)[:300], "ms": int((_t.perf_counter() - t0) * 1000)}


FUND_KEY_FIELDS = ("pe", "pb", "roe", "debt_to_equity", "profit_margin", "revenue_growth", "earnings_growth", "market_cap")


async def _fundamentals_probes(equity: Any, symbol: str) -> list[dict[str, Any]]:
    out = []
    for name, fn in equity.fundamentals_sources(symbol):
        if fn is None:
            out.append({"group": "fundamentals", "source": name, "status": "not set up",
                        "detail": "add its API key to .env" if name in ("finnhub", "alphavantage") else "not applicable to this symbol"})
            continue

        def summarize(d: Any) -> tuple[str, str]:
            got = [k for k in FUND_KEY_FIELDS if d and d.get(k) is not None]
            return ("ok", f"{len(got)}/{len(FUND_KEY_FIELDS)} key fields: {', '.join(got)}") if got else ("empty", "answered, but with no company data")
        out.append(await _probe("fundamentals", name, lambda fn=fn: fn(symbol), summarize))
    return out


@router.get("/fundamentals/{symbol}/sources")
async def fundamentals_sources(symbol: str, principal: CurrentUser) -> dict[str, Any]:
    """Diagnosis: try every fundamentals source for this symbol right now and report what each returned."""
    prov = get_provider()
    equity = getattr(prov, "equity", prov)
    if not hasattr(equity, "fundamentals_sources"):
        return {"symbol": symbol, "mode": getattr(prov, "name", "?"), "sources": [],
                "note": "Simulated market data (MARKET_DATA_PROVIDER=simulated) — no live sources to check."}
    return {"symbol": symbol, "mode": getattr(prov, "name", "?"), "sources": await _fundamentals_probes(equity, symbol)}


@router.get("/sources/test")
async def test_sources(principal: CurrentUser, symbol: str = "RELIANCE.NS", mf: str | None = None) -> dict[str, Any]:
    """Settings → Data sources → "Test all": one real request to every source (prices, history, index, mutual-fund
    NAV, each fundamentals source), with the answer or the exact error — so a broken source is found in seconds."""
    prov = get_provider()
    equity = getattr(prov, "equity", prov)
    live = hasattr(equity, "fundamentals_sources")
    probes = [
        _probe("prices", "Yahoo quote", lambda: prov.quotes([symbol]),
               lambda q: ("ok", f"{symbol} ₹{q[symbol].price:,.2f}") if q.get(symbol) else ("empty", f"no price for {symbol}")),
        _probe("prices", "Yahoo daily history", lambda: prov.history(symbol, 30),
               lambda b: ("ok", f"{len(b)} daily bars, last {b[-1].date}") if b else ("empty", "no bars")),
        _probe("prices", "NIFTY 50 index", lambda: prov.quotes(["^NSEI"]),
               lambda q: ("ok", f"NIFTY {q['^NSEI'].price:,.2f}") if q.get("^NSEI") else ("empty", "no index level")),
    ]
    if mf:
        probes.append(_probe("mutual funds", "AMFI NAV (mfapi.in)", lambda: prov.quotes([mf]),
                             lambda q: ("ok", f"NAV ₹{q[mf].price:,.4f} ({q[mf].ts:%d %b})") if q.get(mf) else ("empty", f"no NAV for scheme {mf}")))
    results = list(await asyncio.gather(*probes))
    if live:
        results += await _fundamentals_probes(equity, symbol)
    return {"symbol": symbol, "mf": mf, "mode": getattr(prov, "name", "?"), "live": live, "results": results,
            "note": None if live else "Simulated market data (MARKET_DATA_PROVIDER=simulated): prices are made up; set it to live in .env."}


@router.get("/news/{symbol}")
async def news(symbol: str, principal: CurrentUser, db: DB) -> list[dict[str, Any]]:
    return await data.news(await _inst(db, symbol, principal))


@router.get("/snapshot/{symbol}")
async def snapshot(symbol: str, principal: CurrentUser, db: DB, days: int = 400) -> dict[str, Any]:
    """Everything analytics/advisor need about one instrument in one call (+ data-quality grade)."""
    inst = await _inst(db, symbol, principal)
    # quotes hit only cache/provider, so they run concurrently with the (single-session) DB work
    quotes_task = asyncio.ensure_future(get_quotes([inst.symbol]) if not inst.asset_type.is_accrual else _none_dict())
    bars = await data.history(db, inst, days)
    fund = await data.fundamentals(db, inst)
    quotes_ = await quotes_task
    q = quotes_.get(inst.symbol)
    return {
        "instrument": instruments.instrument_dict(inst), "quote": q, "bars": bars, "fundamentals": fund,
        "data_quality": quality.assess(inst.asset_type.value, q, bars, fund),
    }


async def _none_dict() -> dict[str, Any]:
    return {}


class ManualPriceIn(BaseModel):
    price_date: date
    close: Decimal = Field(gt=0)


@router.post("/prices/{instrument_id}/manual", status_code=201)
async def manual_price(instrument_id: uuid.UUID, body: ManualPriceIn, principal: CurrentUser, db: DB) -> dict[str, str]:
    """Manual NAV entry (NPS schemes, unlisted, gold) — never overwritten by provider syncs."""
    inst = await db.get(Instrument, instrument_id)
    if inst is None or inst.household_id not in (None, principal.household_id):
        raise HTTPException(404, "Instrument not found")
    await data.upsert_bars(db, inst.id, [{"date": body.price_date, "close": body.close}], source="manual")
    return {"status": "ok"}


@router.get("/corporate-actions")
async def corporate_actions(principal: CurrentUser, db: DB, instrument_ids: str, since: date | None = None) -> list[dict[str, Any]]:
    ids = [uuid.UUID(x) for x in instrument_ids.split(",") if x][:500]
    return await data.corporate_actions(db, ids, since)


class NearIn(BaseModel):
    items: list[dict[str, Any]] = Field(max_length=200)  # {symbol, isin?, date}
    days: int = Field(default=10, ge=0, le=60)


@router.post("/corporate-actions/near")
async def corporate_actions_near(body: NearIn, principal: CurrentUser, db: DB) -> list[dict[str, Any] | None]:
    """For each (symbol, date): a bonus or split with an ex-date within ``days`` — e.g. to tell whether a
    zero-cost lot on a tax statement is bonus shares (cost ₹0 by law) or a transfer with a missing cost."""
    from datetime import timedelta

    out: list[dict[str, Any] | None] = []
    synced: set[uuid.UUID] = set()
    for it in body.items:
        try:
            when = date.fromisoformat(str(it.get("date"))[:10])
        except ValueError:
            out.append(None)
            continue
        base = str(it.get("symbol") or "").upper().removesuffix(".NS").removesuffix(".BO")
        conds = [Instrument.symbol.in_([base, f"{base}.NS", f"{base}.BO"])]
        if it.get("isin"):
            conds.append(Instrument.isin == str(it["isin"]).upper())
        inst = (await db.execute(select(Instrument).where(or_(*conds), Instrument.household_id.is_(None)).limit(1))).scalar_one_or_none()
        if inst is None:
            out.append(None)
            continue
        acts = await data.corporate_actions(db, [inst.id], when - timedelta(days=body.days))
        if not any(a["action_type"] in ("bonus", "split") for a in acts) and inst.id not in synced:
            synced.add(inst.id)
            try:
                await data.sync_corporate_actions(db, inst)
                acts = await data.corporate_actions(db, [inst.id], when - timedelta(days=body.days))
            except Exception as exc:
                log.warning("market.corporate_actions_sync_failed", symbol=inst.symbol, error=str(exc)[:200])
        hit = next((a for a in acts if a["action_type"] in ("bonus", "split")
                    and abs((date.fromisoformat(a["ex_date"]) - when).days) <= body.days), None)
        out.append({**hit, "symbol": inst.symbol} if hit else None)
    return out


@router.get("/overview")
async def overview(principal: CurrentUser) -> dict[str, Any]:
    """Markets tab header: indices, VIX, sector moves, top gainers/losers of the tracked universe."""

    async def load() -> dict[str, Any]:
        idx_syms = [s for s, *_ in INDICES]
        stock_syms = [s for s, _, sector, *_ in STOCKS if sector != "ETF"]
        q = await get_quotes(idx_syms + stock_syms)
        stocks = sorted((q[s] for s in stock_syms if s in q), key=lambda x: x["change_pct"])
        names = {s: n for s, n, *_ in STOCKS} | {s: n for s, n, _ in INDICES}
        for item in stocks:
            item["name"] = names.get(item["symbol"])
        sectors = [{"sector": sec, "symbol": sym, **(q.get(sym) or {})} for sec, sym in SECTOR_INDEX.items()]
        return {
            "indices": [{**q[s], "name": names[s]} for s in idx_syms if s in q and s != "^INDIAVIX"],
            "vix": q.get("^INDIAVIX"),
            "sectors": sorted(sectors, key=lambda x: -(x.get("change_pct") or 0)),
            "gainers": list(reversed(stocks[-5:])),
            "losers": stocks[:5],
            "most_active": sorted(stocks, key=lambda x: -(x.get("volume") or 0))[:5],
        }

    return await cache.get_or_load("market:overview", load, ttl=5, l1_ttl=2)


@ws_router.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket) -> None:
    try:
        await get_ws_principal(websocket)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        await hub.serve(websocket)
    except WebSocketDisconnect:
        pass


async def seed_on_startup() -> None:
    async with SessionLocal() as db:
        n = await instruments.seed_universe(db)
        if n:
            log.info("market.seeded_universe", inserted=n)


async def warm_history() -> None:
    """Market worker, at start: make sure every universe instrument has recent history stored."""
    async with SessionLocal() as db:
        insts = (await db.execute(select(Instrument).where(Instrument.household_id.is_(None)))).scalars().all()
    for inst in insts:
        async with SessionLocal() as db:
            try:
                await data.history(db, inst, 30)
            except Exception as exc:
                log.warning("market.warmup_failed", symbol=inst.symbol, error=str(exc))
