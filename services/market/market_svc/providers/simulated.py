"""Offline Simulator provider — deterministic, factor-based market so every engine (incl. the
Drawdown Sentinel's market/sector/stock attribution) can be exercised with no internet.

Daily log-return of a stock = beta · market + sector factor + idiosyncratic, all generated
from fixed seeds, so the same symbol always has the same history. Live quotes follow a
seeded intraday random walk in 5-second steps, so prices visibly tick.
"""
from __future__ import annotations

import hashlib
import math
import time
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import numpy as np

from .base import Bar, Quote
from .universe import INDICES, MUTUAL_FUNDS, NPS_SCHEMES, SECTOR_INDEX, STOCKS

START = date(2019, 1, 1)
STEP_SECONDS = 5
STEPS_PER_DAY = 86_400 // STEP_SECONDS
MARKET_MU, MARKET_SIGMA = 0.11 / 252, 0.15 / math.sqrt(252)


def _seed(*parts: Any) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


@lru_cache(maxsize=4)
def _calendar(today: date) -> np.ndarray:
    days = np.arange(np.datetime64(START), np.datetime64(today))
    return days[np.is_busday(days)]


@lru_cache(maxsize=4)
def _market_returns(today: date) -> np.ndarray:
    n = len(_calendar(today))
    return np.random.default_rng(_seed("market")).normal(MARKET_MU, MARKET_SIGMA, n)


@lru_cache(maxsize=64)
def _sector_returns(sector: str, today: date) -> np.ndarray:
    n = len(_calendar(today))
    return np.random.default_rng(_seed("sector", sector)).normal(0, 0.007, n)


class _Spec:
    __slots__ = ("symbol", "name", "kind", "base", "drift", "vol", "beta", "sector", "quality", "meta")

    def __init__(self, symbol, name, kind, base, drift, vol, beta, sector=None, quality=0.6, meta=None):
        self.symbol, self.name, self.kind, self.base = symbol, name, kind, base
        self.drift, self.vol, self.beta, self.sector, self.quality = drift, vol, beta, sector, quality
        self.meta = meta or {}


def _build_specs() -> dict[str, _Spec]:
    specs: dict[str, _Spec] = {}
    for sym, name, base in INDICES:
        vol = 0.9 if sym == "^INDIAVIX" else 0.15
        specs[sym] = _Spec(sym, name, "index", base, 0.11, vol, 1.0 if sym != "^INDIAVIX" else -2.0)
    for sym, name, sector, base, drift, vol, q in STOCKS:
        beta = 0.6 + (vol - 0.14) * 2.2
        specs[sym] = _Spec(sym, name, "etf" if sector == "ETF" else "stock", base, drift, vol, beta, sector, q)
    for code, name, cat, plan, er, drift, vol in MUTUAL_FUNDS:
        beta = 0.0 if cat.startswith("debt") else min(1.3, vol / 0.15)
        specs[code] = _Spec(code, name, "mutual_fund", 100.0 if cat.startswith("equity") else 30.0, drift, vol, beta,
                            meta={"mf_category": cat, "plan": plan, "expense_ratio": er})
    for sym, name, cls, drift, vol in NPS_SCHEMES:
        specs[sym] = _Spec(sym, name, "nps", 40.0, drift, vol, 0.9 if cls == "E" else 0.0, meta={"scheme": cls})
    return specs


SPECS = _build_specs()


def _spec(symbol: str) -> _Spec:
    s = SPECS.get(symbol)
    if s is None and symbol.isdigit():  # unknown AMFI code (offline fallback): a daily-NAV equity fund
        h = _seed(symbol) % 1000 / 1000
        s = _Spec(symbol, f"Mutual fund {symbol}", "mutual_fund", 20 + 80 * h, 0.10, 0.14, 0.9, meta={"mf_category": "equity:other"})
    elif s is None:  # unknown symbol: synthesise a plausible mid-cap
        h = _seed(symbol) % 1000 / 1000
        s = _Spec(symbol, symbol, "stock", 100 + 900 * h, 0.05 + 0.1 * h, 0.25 + 0.15 * h, 1.0, "Diversified", 0.3 + 0.5 * h)
    if symbol not in SPECS:
        SPECS[symbol] = s
    return s


@lru_cache(maxsize=512)
def _closes(symbol: str, today: date) -> tuple[np.ndarray, np.ndarray]:
    s = _spec(symbol)
    cal = _calendar(today)
    n = len(cal)
    m = _market_returns(today)
    if symbol == "^NSEI" or (s.kind == "index" and s.symbol not in SECTOR_INDEX.values() and symbol != "^INDIAVIX"):
        r = m
    elif symbol == "^INDIAVIX":
        r = -2.0 * (m - MARKET_MU) + np.random.default_rng(_seed(symbol)).normal(0, 0.03, n)
        r = r - r.mean()  # mean-reverting-ish: no long-run drift
    else:
        daily_vol = s.vol / math.sqrt(252)
        sector_name = s.sector or ""
        sec = _sector_returns(sector_name, today) if s.kind in ("stock", "etf", "index") else np.zeros(n)
        if s.kind == "index":
            sector_name = next((k for k, v in SECTOR_INDEX.items() if v == symbol), symbol)
            sec = _sector_returns(sector_name, today)
        systematic_var = (s.beta * MARKET_SIGMA) ** 2 + (0.007**2 if sec.any() else 0)
        idio_sigma = math.sqrt(max(daily_vol**2 - systematic_var, (0.2 * daily_vol) ** 2))
        idio_mu = s.drift / 252 - s.beta * MARKET_MU
        idio = np.random.default_rng(_seed("idio", symbol)).normal(idio_mu, idio_sigma, n)
        r = s.beta * m + sec + idio
    start_price = s.base / math.exp(s.drift * (n / 252))
    closes = start_price * np.exp(np.cumsum(r))
    return cal, closes


def _intraday_factor(symbol: str, day: date, vol: float, step: int) -> float:
    rng = np.random.default_rng(_seed("intraday", symbol, day.isoformat()))
    sigma = vol / math.sqrt(252) / math.sqrt(STEPS_PER_DAY) * 1.5
    path = np.cumsum(rng.normal(0, sigma, step + 1))
    return float(math.exp(path[-1]))


class SimulatedProvider:
    name = "simulated"

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime.now(UTC)
        today = now.date()
        step = (now.hour * 3600 + now.minute * 60 + now.second) // STEP_SECONDS
        out: dict[str, Quote] = {}
        for sym in symbols:
            s = _spec(sym)
            _, closes = _closes(sym, today)
            prev = float(closes[-1])
            if s.kind in ("mutual_fund", "nps"):  # NAVs don't tick intraday
                price, hi, lo = prev, prev, prev
                prev = float(closes[-2])
            else:
                price = prev * _intraday_factor(sym, today, s.vol, step)
                hi, lo = max(prev, price) * 1.003, min(prev, price) * 0.997
            out[sym] = Quote(sym, round(price, 2), round(prev, 2), round(hi, 2), round(lo, 2),
                             volume=int(_seed(sym, step) % 5_000_000), source=self.name, ts=now)
        return out

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        today = datetime.now(UTC).date()
        cal, closes = _closes(symbol, today)
        cutoff = np.datetime64(today - timedelta(days=days))
        rng = np.random.default_rng(_seed("ohlc", symbol))
        bars: list[Bar] = []
        idx = np.nonzero(cal >= cutoff)[0]
        for i in idx:
            c = float(closes[i])
            o = float(closes[i - 1]) if i > 0 else c
            spread = abs(rng.normal(0, 0.008)) * c
            bars.append(Bar(cal[i].astype(date), round(o, 2), round(max(o, c) + spread, 2), round(min(o, c) - spread, 2),
                            round(c, 2), int(_seed(symbol, i) % 8_000_000)))
        return bars

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        s = _spec(symbol)
        if s.kind not in ("stock",):
            return None
        q = s.quality
        rng = np.random.default_rng(_seed("fund", symbol))
        j = lambda scale: float(rng.normal(0, scale))  # noqa: E731
        _, closes = _closes(symbol, datetime.now(UTC).date())
        last = float(closes[-1])
        pe = max(6.0, 14 + 40 * q + j(6))
        return {
            "name": s.name, "sector": s.sector, "industry": s.sector,
            "market_cap": round(last * (5e8 + 5e9 * q), 0),
            "pe": round(pe, 2), "pe_5y_median": round(pe * (0.9 + 0.3 * (1 - q) + j(0.05)), 2),
            "sector_pe": round(24 + j(4), 2), "pb": round(max(0.5, 1 + 9 * q + j(1)), 2),
            "peg": round(max(0.3, 2.5 - 1.5 * q + j(0.3)), 2), "eps": round(last / pe, 2),
            "roe": round(6 + 22 * q + j(2), 2), "roce": round(8 + 24 * q + j(2), 2),
            "debt_to_equity": round(max(0.0, 1.8 * (1 - q) + j(0.2)), 2),
            "current_ratio": round(0.9 + 1.2 * q + j(0.1), 2),
            "interest_coverage": round(max(0.5, 2 + 25 * q + j(2)), 2),
            "profit_margin": round(3 + 20 * q + j(2), 2), "operating_margin": round(6 + 22 * q + j(2), 2),
            "revenue_growth": round(-2 + 20 * q + j(4), 2), "earnings_growth": round(-8 + 30 * q + j(6), 2),
            "revenue_cagr_3y": round(2 + 16 * q + j(2), 2), "eps_cagr_3y": round(-4 + 24 * q + j(3), 2),
            "dividend_yield": round(max(0.0, 0.3 + 1.5 * q + j(0.4)), 2), "payout_ratio": round(15 + 30 * q, 1),
            "beta": round(s.beta, 2),
            "promoter_holding": round(35 + 30 * q + j(5), 2),
            "promoter_pledge": round(max(0.0, 25 * (1 - q) ** 3 + j(1)), 2),
            "institutional_holding": round(20 + 25 * q + j(3), 2),
            "fifty_two_week_high": round(float(closes[-252:].max()), 2),
            "fifty_two_week_low": round(float(closes[-252:].min()), 2),
            "book_value": round(last / max(0.5, 1 + 9 * q), 2),
        }

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        s = _spec(symbol)
        now = int(time.time())
        templates = [
            "{n}: analysts review quarterly outlook", "{n} shares move with {sec} peers",
            "{n} management commentary on demand trends", "Institutional activity seen in {n}",
            "{sec} sector update: what it means for {n}",
        ]
        return [
            {"title": t.format(n=s.name, sec=s.sector or "market"), "publisher": "Aquilvyn Simulator",
             "link": None, "published_at": datetime.fromtimestamp(now - i * 5400, UTC).isoformat(), "simulated": True}
            for i, t in enumerate(templates[:limit])
        ]

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [s for s in SPECS.values() if q in s.symbol.lower() or q in s.name.lower()]
        return [{"symbol": s.symbol, "name": s.name, "asset_type": s.kind, "sector": s.sector, "meta": s.meta} for s in hits[:limit]]

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        s = _spec(symbol)
        if s.kind != "stock":
            return []
        today = datetime.now(UTC).date()
        out = [{"action_type": "dividend", "ex_date": date(y, 7, 15).isoformat(), "amount": round(s.base * 0.008, 2)}
               for y in range(today.year - 2, today.year + (1 if today >= date(today.year, 7, 15) else 0))]
        return out
