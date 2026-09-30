"""Optional key-based fundamentals sources, used only when their API key is set in .env:

* Finnhub (FINNHUB_API_KEY)            — /stock/metric: valuation, returns, margins, growth, balance-sheet ratios
* Alpha Vantage (ALPHAVANTAGE_API_KEY) — OVERVIEW: the same, 25 requests/day on the free plan

Free plans of both mostly cover US listings; for NSE/BSE fundamentals they generally need a paid plan. They
sit after Yahoo and before NSE in the chain and only fill fields that are still missing.
"""
from __future__ import annotations

from typing import Any

import httpx


def _f(v: Any, scale: float = 1.0) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x * scale, 4) if x == x else None


async def finnhub(symbol: str, key: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://finnhub.io/api/v1/stock/metric", params={"symbol": symbol, "metric": "all", "token": key})
        r.raise_for_status()
        m = (r.json() or {}).get("metric") or {}
    if not m:
        return None
    de = _f(m.get("totalDebt/totalEquityQuarterly") or m.get("totalDebt/totalEquityAnnual"))
    return {
        "pe": _f(m.get("peTTM") or m.get("peBasicExclExtraTTM")), "pb": _f(m.get("pbQuarterly") or m.get("pbAnnual")),
        "roe": _f(m.get("roeTTM")), "debt_to_equity": de,
        "profit_margin": _f(m.get("netProfitMarginTTM")), "operating_margin": _f(m.get("operatingMarginTTM")),
        "revenue_growth": _f(m.get("revenueGrowthTTMYoy")), "earnings_growth": _f(m.get("epsGrowthTTMYoy")),
        "market_cap": _f(m.get("marketCapitalization"), 1e6), "beta": _f(m.get("beta")),
        "dividend_yield": _f(m.get("dividendYieldIndicatedAnnual"), 0.01), "current_ratio": _f(m.get("currentRatioQuarterly")),
        "book_value": _f(m.get("bookValuePerShareQuarterly")),
        "fifty_two_week_high": _f(m.get("52WeekHigh")), "fifty_two_week_low": _f(m.get("52WeekLow")),
    }


async def alphavantage(symbol: str, key: str) -> dict[str, Any] | None:
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    av_symbol = f"{base}.BSE" if symbol.upper().endswith((".NS", ".BO")) else base
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://www.alphavantage.co/query", params={"function": "OVERVIEW", "symbol": av_symbol, "apikey": key})
        r.raise_for_status()
        d = r.json() or {}
    if not d.get("Symbol"):
        if d.get("Note") or d.get("Information"):
            raise RuntimeError(str(d.get("Note") or d.get("Information"))[:160])  # rate limit / plan message
        return None
    return {
        "name": d.get("Name"), "sector": d.get("Sector"), "industry": d.get("Industry"),
        "pe": _f(d.get("PERatio")), "pb": _f(d.get("PriceToBookRatio")), "peg": _f(d.get("PEGRatio")), "eps": _f(d.get("EPS")),
        "roe": _f(d.get("ReturnOnEquityTTM"), 100), "profit_margin": _f(d.get("ProfitMargin"), 100),
        "operating_margin": _f(d.get("OperatingMarginTTM"), 100), "revenue_growth": _f(d.get("QuarterlyRevenueGrowthYOY"), 100),
        "earnings_growth": _f(d.get("QuarterlyEarningsGrowthYOY"), 100), "market_cap": _f(d.get("MarketCapitalization")),
        "beta": _f(d.get("Beta")), "dividend_yield": _f(d.get("DividendYield")), "book_value": _f(d.get("BookValue")),
        "fifty_two_week_high": _f(d.get("52WeekHigh")), "fifty_two_week_low": _f(d.get("52WeekLow")),
    }
