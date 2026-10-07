"""Technical indicator primitives (pure numpy, no look-ahead). Arrays are oldest → newest;
leading values that can't be computed are NaN."""
from __future__ import annotations

import numpy as np

Arr = np.ndarray


def sma(x: Arr, n: int) -> Arr:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) >= n:
        c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def ema(x: Arr, n: int) -> Arr:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < n:
        return out
    alpha = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(x: Arr, n: int = 14) -> Arr:
    """Wilder's RSI."""
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) <= n:
        return out
    d = np.diff(x.astype(float))
    gain, loss = np.clip(d, 0, None), np.clip(-d, 0, None)
    avg_g, avg_l = gain[:n].mean(), loss[:n].mean()
    out[n] = 100 - 100 / (1 + avg_g / avg_l) if avg_l else 100.0
    for i in range(n + 1, len(x)):
        avg_g = (avg_g * (n - 1) + gain[i - 1]) / n
        avg_l = (avg_l * (n - 1) + loss[i - 1]) / n
        out[i] = 100 - 100 / (1 + avg_g / avg_l) if avg_l else 100.0
    return out


def macd(x: Arr, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Arr, Arr, Arr]:
    line = ema(x, fast) - ema(x, slow)
    sig = np.full_like(line, np.nan)
    valid = ~np.isnan(line)
    if valid.sum() >= signal:
        sig[valid] = ema(line[valid], signal)
    return line, sig, line - sig


def bollinger(x: Arr, n: int = 20, k: float = 2.0) -> tuple[Arr, Arr, Arr]:
    mid = sma(x, n)
    sd = np.full_like(x, np.nan, dtype=float)
    for i in range(n - 1, len(x)):
        sd[i] = x[i - n + 1 : i + 1].std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def atr(high: Arr, low: Arr, close: Arr, n: int = 14) -> Arr:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    out = np.full_like(close, np.nan, dtype=float)
    if len(close) <= n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(close)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def stochastic(high: Arr, low: Arr, close: Arr, n: int = 14, d: int = 3) -> tuple[Arr, Arr]:
    k = np.full_like(close, np.nan, dtype=float)
    for i in range(n - 1, len(close)):
        hh, ll = high[i - n + 1 : i + 1].max(), low[i - n + 1 : i + 1].min()
        k[i] = 100 * (close[i] - ll) / (hh - ll) if hh > ll else 50.0
    return k, sma(np.nan_to_num(k, nan=50.0), d)


def max_drawdown(x: Arr) -> tuple[float, int, int]:
    """(max drawdown as a negative fraction, peak index, trough index)."""
    if len(x) == 0:
        return 0.0, 0, 0
    peaks = np.maximum.accumulate(x)
    dd = x / peaks - 1
    trough = int(np.argmin(dd))
    peak = int(np.argmax(x[: trough + 1])) if trough else 0
    return float(dd[trough]), peak, trough


def support_resistance(high: Arr, low: Arr, close: Arr, window: int = 5, lookback: int = 120) -> dict[str, list[float]]:
    """Swing pivots over the lookback, clustered within 1.5 %, nearest first."""
    h, lo, c = high[-lookback:], low[-lookback:], close[-1]
    pivots_hi = [h[i] for i in range(window, len(h) - window) if h[i] == h[i - window : i + window + 1].max()]
    pivots_lo = [lo[i] for i in range(window, len(lo) - window) if lo[i] == lo[i - window : i + window + 1].min()]

    def cluster(levels: list[float]) -> list[float]:
        out: list[list[float]] = []
        for lv in sorted(levels):
            if out and abs(lv / np.mean(out[-1]) - 1) < 0.015:
                out[-1].append(lv)
            else:
                out.append([lv])
        return [round(float(np.mean(g)), 2) for g in out]

    levels = cluster(pivots_hi + pivots_lo)
    return {
        "support": sorted([lv for lv in levels if lv < c], reverse=True)[:3],
        "resistance": sorted([lv for lv in levels if lv > c])[:3],
    }


def last(x: Arr) -> float | None:
    v = x[-1] if len(x) else np.nan
    return None if np.isnan(v) else round(float(v), 4)
