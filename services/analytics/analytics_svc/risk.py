"""Risk Engine (E15) + return attribution used by the Drawdown Sentinel (E20)."""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import indicators as ind


def _aligned_returns(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    bm = {x["date"]: x["close"] for x in b}
    pairs = [(x["close"], bm[x["date"]]) for x in a if x["date"] in bm]
    if len(pairs) < 3:
        return np.array([]), np.array([])
    arr = np.array(pairs, dtype=float)
    return np.diff(np.log(arr[:, 0])), np.diff(np.log(arr[:, 1]))


def beta_corr(bars: list[dict[str, Any]], bench: list[dict[str, Any]], window: int = 252) -> dict[str, float | None]:
    ra, rb = _aligned_returns(bars[-window - 1 :], bench[-window - 1 :])
    if len(ra) < 40 or np.var(rb) == 0:
        return {"beta": None, "correlation": None}
    beta = float(np.cov(ra, rb)[0, 1] / np.var(rb, ddof=1))
    return {"beta": round(beta, 3), "correlation": round(float(np.corrcoef(ra, rb)[0, 1]), 3)}


def stats(bars: list[dict[str, Any]], rf: float = 0.065) -> dict[str, Any]:
    if len(bars) < 30:
        return {"available": False}
    c = np.array([b["close"] for b in bars], dtype=float)
    r = np.diff(np.log(c))
    vol = float(r.std(ddof=1) * math.sqrt(252))
    years = len(r) / 252
    cagr = float((c[-1] / c[0]) ** (1 / years) - 1) if years > 0 else 0.0
    downside = r[r < 0]
    dvol = float(downside.std(ddof=1) * math.sqrt(252)) if len(downside) > 2 else None
    mdd, _, _ = ind.max_drawdown(c)
    return {
        "available": True, "volatility_pct": round(vol * 100, 2), "cagr_pct": round(cagr * 100, 2),
        "sharpe": round((cagr - rf) / vol, 2) if vol else None,
        "sortino": round((cagr - rf) / dvol, 2) if dvol else None,
        "max_drawdown_pct": round(mdd * 100, 2),
        "var_95_1d_pct": round(float(np.percentile(r, 5)) * 100, 2),
    }


def attribute_move(
    bars: list[dict[str, Any]], market: list[dict[str, Any]], sector: list[dict[str, Any]] | None, since_date: str
) -> dict[str, Any]:
    """Split the holding's log-return since ``since_date`` into market / sector / stock-specific.

    stock ≈ β · market + (sector − market) + residual, β estimated on the prior year.
    Returned in % of price so the report can say e.g. "−22%: −6% market, −4% sector, −12% company"."""
    def since(x: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [b for b in x if b["date"] >= since_date]

    hist = [b for b in bars if b["date"] < since_date][-253:]
    s_bars, m_bars = since(bars), since(market)
    if len(s_bars) < 2 or len(m_bars) < 2:
        return {"available": False, "reason": "Not enough overlapping history"}
    total = math.log(s_bars[-1]["close"] / s_bars[0]["close"])
    mkt = math.log(m_bars[-1]["close"] / m_bars[0]["close"])
    bm = beta_corr(hist, [b for b in market if b["date"] < since_date][-253:])["beta"] or 1.0
    market_part = bm * mkt
    sector_part = 0.0
    if sector:
        sc_bars = since(sector)
        if len(sc_bars) >= 2:
            # sector's move in excess of the market (stock assumed to load 1:1 on its sector)
            sector_part = math.log(sc_bars[-1]["close"] / sc_bars[0]["close"]) - mkt
    specific = total - market_part - sector_part
    pct = lambda v: round((math.exp(v) - 1) * 100, 2)  # noqa: E731
    parts = {"market": market_part, "sector": sector_part, "stock_specific": specific}
    dominant = max(parts, key=lambda k: abs(parts[k]))
    return {
        "available": True, "since": since_date, "total_pct": pct(total), "market_pct": pct(market_part),
        "sector_pct": pct(sector_part), "stock_specific_pct": pct(specific), "market_beta": round(bm, 2),
        "dominant_driver": dominant,
        "share_stock_specific": round(abs(specific) / (abs(market_part) + abs(sector_part) + abs(specific) or 1), 2),
    }
