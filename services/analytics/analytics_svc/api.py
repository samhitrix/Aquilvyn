from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from fm_common import http
from fm_common.cache import cache
from fm_common.config import settings
from fm_common.deps import CurrentUser
from fm_common.logging import get_logger

from . import fundamental, mutual_fund, regime, risk, technical

log = get_logger(__name__)

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


PEER_CAP = 45  # funds scored per category (the largest categories have ~40 direct-growth funds)


def peer_row(code: str, name: str, a: dict[str, Any]) -> dict[str, Any] | None:
    """One fund's line in a category ranking — only funds with a score from at least a year of NAVs."""
    f = (a or {}).get("fund") or {}
    if not f.get("available") or f.get("score") is None:
        return None
    r3 = (f.get("rolling") or {}).get("3y") or {}
    return {"code": code, "name": name, "score": f["score"], "consistency_pct": f.get("consistency_pct"), "alpha_pct": f.get("alpha_pct"),
            "return_3y_pct": r3.get("fund_avg_pct"), "cagr_pct": (f.get("risk") or {}).get("cagr_pct"),
            "rolling_years": 3 if r3 else 1, "verdict": f.get("verdict")}


def rank(rows: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Best first: fund score, then rolling-period consistency vs the benchmark, then 3-year return."""
    return sorted((r for r in rows if r), key=lambda r: (r["score"], r.get("consistency_pct") or 0, r.get("return_3y_pct") or 0), reverse=True)


async def _category_ranking(code: str, token: str) -> dict[str, Any]:
    peers = await http.get(MARKET(), f"/api/v1/market/funds/peers/{code}", token=token)
    if not peers.get("category"):
        return {"category": None, "label": None, "ranking": [], "self": None, "of": 0}
    todo = [(code, (peers.get("self") or {}).get("name") or code)] + [(p["code"], p["name"]) for p in peers["peers"][:PEER_CAP]]
    sem = asyncio.Semaphore(5)

    async def one(c: str, n: str) -> dict[str, Any] | None:
        async with sem:
            try:
                a = await cache.get_or_load(f"an:peerfund:{c}", lambda: analyse_symbol(c, token), ttl=86400, l1_ttl=600)
            except Exception as exc:  # a fund without NAV history just isn't ranked
                log.info("analysis.peer_failed", code=c, error=str(exc)[:120])
                return None
            return peer_row(c, n, a)

    rows = rank(await asyncio.gather(*(one(c, n) for c, n in todo)))
    me = next((i for i, r in enumerate(rows) if r["code"] == code), None)
    return {"category": peers["category"], "label": peers["label"], "ranking": [r for r in rows if r["code"] != code],
            "self": rows[me] if me is not None else None, "self_rank": me + 1 if me is not None else None, "of": len(rows)}


@router.get("/mf/peers/{code}")
async def mf_peers(code: str, principal: CurrentUser, limit: int = Query(5, ge=1, le=20)) -> dict[str, Any]:
    """The best funds in a mutual fund's own AMFI category right now, scored like your funds (rolling-return
    consistency vs benchmark, alpha, risk-adjusted return, cost) — what to switch into. Cached a day per fund."""
    res = await cache.get_or_load(f"an:peers:{code}", lambda: _category_ranking(code, principal.token), ttl=86400, l1_ttl=600)
    return {**res, "ranking": res["ranking"][:limit]}


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
