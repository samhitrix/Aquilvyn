"""Technical Analysis Engine (E12) → a 0-100 technical score + signals with evidence."""
from __future__ import annotations

from typing import Any

import numpy as np

from . import indicators as ind


def analyse(bars: list[dict[str, Any]], benchmark_bars: list[dict[str, Any]] | None = None, live_price: float | None = None) -> dict[str, Any]:
    if len(bars) < 30:
        return {"available": False, "reason": f"Need ≥30 bars, have {len(bars)}"}
    if live_price:
        from datetime import date

        today = date.today().isoformat()
        live_bar = {"date": today, "close": live_price, "high": live_price, "low": live_price, "volume": bars[-1].get("volume") or 0}
        bars = bars[:-1] + [live_bar] if bars[-1]["date"] >= today else bars + [live_bar]
    close = np.array([b["close"] for b in bars], dtype=float)
    high = np.array([b.get("high") or b["close"] for b in bars], dtype=float)
    low = np.array([b.get("low") or b["close"] for b in bars], dtype=float)
    vol = np.array([b.get("volume") or 0 for b in bars], dtype=float)
    price = float(close[-1])

    s20, s50, s200 = ind.sma(close, 20), ind.sma(close, 50), ind.sma(close, 200)
    e21 = ind.ema(close, 21)
    r = ind.rsi(close)
    m_line, m_sig, m_hist = ind.macd(close)
    bb_u, bb_m, bb_l = ind.bollinger(close)
    a = ind.atr(high, low, close)
    k, d = ind.stochastic(high, low, close)
    look = close[-252:]
    hi52, lo52 = float(look.max()), float(look.min())
    hi52_date = bars[-len(look) + int(look.argmax())]["date"]
    mdd, _, _ = ind.max_drawdown(look)
    # recent structure — for trailing stops on trades and "has it stopped falling?" before buying a dip
    hi22 = float(close[-22:].max())
    last20 = close[-20:]
    lo20 = float(last20.min())
    days_since_lo20 = int(len(last20) - 1 - int(last20.argmin()))
    atr_now = None if np.isnan(a[-1]) else float(a[-1])

    values = {
        "price": round(price, 2), "sma20": ind.last(s20), "sma50": ind.last(s50), "sma200": ind.last(s200),
        "ema21": ind.last(e21), "rsi14": ind.last(r), "macd": ind.last(m_line), "macd_signal": ind.last(m_sig),
        "macd_hist": ind.last(m_hist), "bb_upper": ind.last(bb_u), "bb_lower": ind.last(bb_l), "atr14": ind.last(a),
        "atr_pct": round(float(a[-1]) / price * 100, 2) if not np.isnan(a[-1]) else None,
        "stoch_k": ind.last(k), "stoch_d": ind.last(d), "high_52w": round(hi52, 2), "high_52w_date": hi52_date, "low_52w": round(lo52, 2),
        "pct_from_52w_high": round((price / hi52 - 1) * 100, 2), "pct_from_52w_low": round((price / lo52 - 1) * 100, 2),
        "max_drawdown_1y_pct": round(mdd * 100, 2),
        "high_22d": round(hi22, 2), "low_20d": round(lo20, 2), "days_since_20d_low": days_since_lo20,
        # Chandelier exit: highest close of the last 22 sessions minus 3 × ATR(14) — a stop that trails the trend
        "chandelier_stop": round(hi22 - 3 * atr_now, 2) if atr_now else None,
        "return_1m_pct": _ret(close, 21), "return_3m_pct": _ret(close, 63), "return_6m_pct": _ret(close, 126), "return_1y_pct": _ret(close, 252),
        "volume_ratio_20d": round(float(vol[-5:].mean() / vol[-25:-5].mean()), 2) if vol[-25:-5].mean() > 0 else None,
    }

    signals: list[dict[str, Any]] = []

    def sig(code: str, label: str, bias: int, weight: float, evidence: str) -> None:
        signals.append({"code": code, "label": label, "bias": bias, "weight": weight, "evidence": evidence})

    sma50, sma200 = values["sma50"], values["sma200"]
    if sma200:
        if price > sma200:
            sig("above_200dma", "Above 200-DMA (long-term uptrend)", 1, 2.0, f"₹{price:,.2f} > 200-DMA ₹{sma200:,.2f}")
        else:
            sig("below_200dma", "Below 200-DMA (long-term downtrend)", -1, 2.0, f"₹{price:,.2f} < 200-DMA ₹{sma200:,.2f}")
        if sma50 and len(s50) > 10 and not np.isnan(s50[-10]) and not np.isnan(s200[-10]):
            if s50[-10] <= s200[-10] and sma50 > sma200:
                sig("golden_cross", "Golden cross in last 10 sessions", 1, 1.5, f"50-DMA {sma50:,.2f} crossed above 200-DMA {sma200:,.2f}")
            elif s50[-10] >= s200[-10] and sma50 < sma200:
                sig("death_cross", "Death cross in last 10 sessions", -1, 1.5, f"50-DMA {sma50:,.2f} crossed below 200-DMA {sma200:,.2f}")
    if sma50:
        bias = 1 if price > sma50 else -1
        sig("vs_50dma", f"{'Above' if bias > 0 else 'Below'} 50-DMA (medium trend)", bias, 1.0, f"50-DMA ₹{sma50:,.2f}")
    if (rv := values["rsi14"]) is not None:
        if rv >= 70:
            sig("rsi_overbought", "RSI overbought", -1, 0.8, f"RSI(14) = {rv:.1f} ≥ 70")
        elif rv <= 30:
            sig("rsi_oversold", "RSI oversold", 1, 0.8, f"RSI(14) = {rv:.1f} ≤ 30")
        else:
            sig("rsi_neutral", "RSI neutral", 1 if rv > 50 else -1, 0.4, f"RSI(14) = {rv:.1f}")
    if values["macd_hist"] is not None:
        bias = 1 if values["macd_hist"] > 0 else -1
        sig("macd", f"MACD {'bullish' if bias > 0 else 'bearish'}", bias, 1.0, f"MACD {values['macd']:.2f} vs signal {values['macd_signal']:.2f}")
    if values["bb_lower"] and price < values["bb_lower"]:
        sig("bb_below", "Closed below lower Bollinger band", -1, 0.5, f"₹{price:,.2f} < ₹{values['bb_lower']:,.2f}")
    elif values["bb_upper"] and price > values["bb_upper"]:
        sig("bb_above", "Closed above upper Bollinger band", 1, 0.5, f"₹{price:,.2f} > ₹{values['bb_upper']:,.2f}")
    if values["pct_from_52w_high"] >= -3:
        sig("near_52w_high", "Near 52-week high (strength)", 1, 0.8, f"{values['pct_from_52w_high']:.1f}% from 52w high")
    elif values["pct_from_52w_high"] <= -30:
        sig("far_from_high", "More than 30% below 52-week high", -1, 1.0, f"{values['pct_from_52w_high']:.1f}% from 52w high")

    rs = None
    if benchmark_bars and len(benchmark_bars) >= 64:
        bc = np.array([b["close"] for b in benchmark_bars], dtype=float)
        rs = round((_ret(close, 63) or 0) - (_ret(bc, 63) or 0), 2)
        values["relative_strength_3m_pct"] = rs
        bias = 1 if rs > 0 else -1
        sig("relative_strength", f"{'Out' if bias > 0 else 'Under'}performing benchmark (3M)", bias, 1.2, f"{rs:+.1f} pp vs benchmark over 3 months")

    total_w = sum(s["weight"] for s in signals) or 1
    raw = sum(s["bias"] * s["weight"] for s in signals) / total_w  # -1..1
    score = round((raw + 1) * 50, 1)
    verdict = "strong_bullish" if score >= 75 else "bullish" if score >= 58 else "neutral" if score >= 42 else "bearish" if score >= 25 else "strong_bearish"
    trend = "uptrend" if sma200 and sma50 and price > sma50 > sma200 else "downtrend" if sma200 and sma50 and price < sma50 < sma200 else "sideways"
    return {
        "available": True, "score": score, "verdict": verdict, "trend": trend, "values": values, "signals": signals,
        "levels": ind.support_resistance(high, low, close),
        "series": {  # compact overlays for charts (last 250 points)
            "sma50": _tail(s50), "sma200": _tail(s200), "bb_upper": _tail(bb_u), "bb_lower": _tail(bb_l),
            "rsi": _tail(r), "macd": _tail(m_line), "macd_signal": _tail(m_sig), "macd_hist": _tail(m_hist),
        },
    }


def _ret(x: np.ndarray, n: int) -> float | None:
    return round(float(x[-1] / x[-n - 1] - 1) * 100, 2) if len(x) > n else None


def _tail(x: np.ndarray, n: int = 250) -> list[float | None]:
    return [None if np.isnan(v) else round(float(v), 3) for v in x[-n:]]
