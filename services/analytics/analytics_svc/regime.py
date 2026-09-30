"""Market Regime Engine (E36): BULL / SIDEWAYS / BEAR (+ CORRECTION, EUPHORIA) from the
Nifty trend, drawdown, momentum, breadth and India VIX. The Advisor tilts its rulebook by
regime so it doesn't panic-sell quality in a correction or chase euphoria."""
from __future__ import annotations

from typing import Any

import numpy as np

from . import indicators as ind


def classify(nifty: list[dict[str, Any]], vix: list[dict[str, Any]] | None, breadth_above_200dma_pct: float | None) -> dict[str, Any]:
    if len(nifty) < 210:
        return {"regime": "unknown", "reason": "insufficient index history"}
    c = np.array([b["close"] for b in nifty], dtype=float)
    s50, s200 = ind.sma(c, 50)[-1], ind.sma(c, 200)[-1]
    s200_prev = ind.sma(c, 200)[-21]
    price = c[-1]
    dd = (price / c[-252:].max() - 1) * 100
    r3m = (price / c[-64] - 1) * 100
    r1y = (price / c[-253] - 1) * 100 if len(c) > 253 else None
    rsi = ind.rsi(c)[-1]
    vix_now = vix[-1]["close"] if vix else None
    vix_avg = float(np.mean([b["close"] for b in vix[-252:]])) if vix else None

    evidence = [
        f"NIFTY {price:,.0f} vs 50-DMA {s50:,.0f} / 200-DMA {s200:,.0f}",
        f"200-DMA slope (1M): {(s200 / s200_prev - 1) * 100:+.2f}%",
        f"Drawdown from 52w high: {dd:.1f}%", f"3M return: {r3m:+.1f}%",
    ]
    if vix_now is not None:
        evidence.append(f"India VIX {vix_now:.1f} (1Y avg {vix_avg:.1f})")
    if breadth_above_200dma_pct is not None:
        evidence.append(f"{breadth_above_200dma_pct:.0f}% of tracked stocks above their 200-DMA")

    if dd <= -20 and price < s200:
        regime = "bear"
    elif dd <= -10:
        regime = "correction"
    elif price > s200 and s50 > s200 and s200 > s200_prev:
        regime = "euphoria" if (rsi >= 72 and r3m >= 12) or (r1y is not None and r1y >= 35) else "bull"
    else:
        regime = "sideways"

    playbook = {
        "bull": "Trend is up: let winners run, add to quality on pullbacks, trim only on valuation extremes.",
        "euphoria": "Market is stretched: book partial profits in over-owned/overvalued names, avoid chasing, keep SIPs.",
        "sideways": "Range-bound: be selective, prefer quality and valuation comfort, use ranges for trades.",
        "correction": "Healthy fall inside a larger trend: don't panic-sell quality CORE holdings; accumulate in tranches.",
        "bear": "Primary downtrend: protect capital on TRADE positions with stops, keep CORE quality, continue SIPs, avoid leverage.",
        "unknown": "Regime unavailable.",
    }[regime]
    return {
        "regime": regime, "playbook": playbook, "evidence": evidence,
        "metrics": {"nifty": round(price, 2), "sma50": round(float(s50), 2), "sma200": round(float(s200), 2), "drawdown_pct": round(dd, 2),
                    "return_3m_pct": round(r3m, 2), "return_1y_pct": round(r1y, 2) if r1y is not None else None, "rsi14": round(float(rsi), 1),
                    "vix": vix_now, "vix_1y_avg": round(vix_avg, 2) if vix_avg else None, "breadth_above_200dma_pct": breadth_above_200dma_pct},
    }
