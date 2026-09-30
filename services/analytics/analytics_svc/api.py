from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from fm_common import http
from fm_common.cache import cache
from fm_common.config import settings
from fm_common.deps import CurrentUser

from . import fundamental, mutual_fund, regime, risk, technical

router = APIRouter(prefix="/analysis", tags=["analysis"])
MARKET = lambda: settings.market_url  # noqa: E731
SECTOR_INDEX = {
    "Financial Services": "^NSEBANK", "Information Technology": "^CNXIT", "Healthcare": "^CNXPHARMA",
    "Automobile": "^CNXAUTO", "FMCG": "^CNXFMCG", "Metals & Mining": "^CNXMETAL",
}


async def snapshot(symbol: str, token: str, days: int = 800) -> dict[str, Any]:
    return await cache.get_or_load(
        f"an:snap:{symbol}:{days}",
        lambda: http.get(MARKET(), f"/api/v1/market/snapshot/{symbol}", token=token, params={"days": days}, request_timeout=45),  # cold: history + fundamentals chain (≤ 25 s)
        ttl=30, l1_ttl=10,
    )


async def analyse_symbol(symbol: str, token: str) -> dict[str, Any]:
    snap = await snapshot(symbol, token)
    inst = snap["instrument"]
    kind = inst["asset_type"]
    sector_sym = SECTOR_INDEX.get(inst.get("sector") or "")
    bench_sym = inst.get("benchmark_symbol") or ("^NSEI" if kind in ("stock", "etf", "reit") else None)
    wanted = {s for s in ("^NSEI", bench_sym, sector_sym) if s and s != symbol}
    others = dict(zip(wanted, await asyncio.gather(*(snapshot(s, token) for s in wanted)), strict=True))
    market_bars = others.get("^NSEI", snap if symbol == "^NSEI" else {}).get("bars", [])
    bench_bars = others.get(bench_sym, {}).get("bars") if bench_sym else None
    bars = snap["bars"]
    live = (snap.get("quote") or {}).get("price")

    result: dict[str, Any] = {
        "instrument": inst, "quote": snap.get("quote"), "data_quality": snap["data_quality"],
        "risk": risk.stats(bars[-756:]),
        "market_beta": risk.beta_corr(bars, market_bars) if market_bars else {"beta": None, "correlation": None},
        "benchmark_symbol": bench_sym, "sector_index": sector_sym,
    }
    if kind in ("stock", "etf", "reit", "index", "gold"):
        result["technical"] = technical.analyse(bars[-400:], (bench_bars or market_bars)[-400:] if (bench_bars or market_bars) else None, live)
    if kind == "stock":
        result["fundamental"] = fundamental.analyse(snap.get("fundamentals"), inst.get("sector"))
    if kind in ("mutual_fund", "nps"):
        meta = dict(inst.get("meta") or {})
        name = (inst.get("name") or "").lower()
        if "direct" in name or ("regular" not in name and meta.get("plan") == "regular"):
            meta["plan"] = "direct" if "direct" in name else None  # never assume Regular without the name saying so
        result["fund"] = mutual_fund.analyse(bars, bench_bars, meta)
        result["technical"] = technical.analyse(bars[-400:], None, None)
    return result


@router.get("/instrument/{symbol}")
async def instrument(symbol: str, principal: CurrentUser) -> dict[str, Any]:
    return await cache.get_or_load(f"an:inst:{symbol}", lambda: analyse_symbol(symbol, principal.token), ttl=60, l1_ttl=15)


class AttributionIn(BaseModel):
    symbol: str
    since_date: str


@router.post("/attribution")
async def attribution(body: AttributionIn, principal: CurrentUser) -> dict[str, Any]:
    snap = await snapshot(body.symbol, principal.token)
    sector_sym = SECTOR_INDEX.get(snap["instrument"].get("sector") or "")
    market = await snapshot("^NSEI", principal.token)
    sector = await snapshot(sector_sym, principal.token) if sector_sym else None
    return risk.attribute_move(snap["bars"], market["bars"], sector["bars"] if sector else None, body.since_date)


@router.get("/regime")
async def market_regime(principal: CurrentUser) -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        nifty, vix = await asyncio.gather(snapshot("^NSEI", principal.token), snapshot("^INDIAVIX", principal.token))
        overview = await http.get(MARKET(), "/api/v1/market/overview", token=principal.token)
        universe = [x["symbol"] for x in overview.get("gainers", []) + overview.get("losers", [])]
        snaps = await asyncio.gather(*(snapshot(s, principal.token, 400) for s in universe), return_exceptions=True)
        above = [
            s["bars"][-1]["close"] > sum(b["close"] for b in s["bars"][-200:]) / 200
            for s in snaps if isinstance(s, dict) and len(s.get("bars", [])) >= 200
        ]
        breadth = round(sum(above) / len(above) * 100, 1) if above else None
        return regime.classify(nifty["bars"], vix["bars"], breadth)

    return await cache.get_or_load("an:regime", load, ttl=900, l1_ttl=120)
