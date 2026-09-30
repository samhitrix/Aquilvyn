import numpy as np
import pytest

from analytics_svc import indicators as ind
from analytics_svc.risk import attribute_move


def test_sma_matches_manual():
    x = np.arange(1, 11, dtype=float)
    s = ind.sma(x, 3)
    assert np.isnan(s[:2]).all()
    assert s[2] == pytest.approx(2.0) and s[-1] == pytest.approx(9.0)


def test_rsi_bounds_and_extremes():
    up = np.arange(1, 40, dtype=float)
    assert ind.rsi(up)[-1] == pytest.approx(100.0)
    zigzag = np.array([10, 11] * 30, dtype=float)
    assert 40 < ind.rsi(zigzag)[-1] < 60


def test_ema_converges_to_constant():
    assert ind.ema(np.full(50, 7.0), 10)[-1] == pytest.approx(7.0)


def test_max_drawdown():
    dd, peak, trough = ind.max_drawdown(np.array([100, 120, 90, 110, 60, 80], dtype=float))
    assert dd == pytest.approx(-0.5) and peak == 1 and trough == 4


def test_macd_signal_lengths():
    x = np.linspace(100, 200, 120)
    line, sig, hist = ind.macd(x)
    assert len(line) == len(sig) == len(hist) == 120
    assert line[-1] > 0  # uptrend → fast EMA above slow EMA


def _bars(returns, start="2025-01-01"):
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    out, p = [], 100.0
    for i, r in enumerate(returns):
        p *= 1 + r
        out.append({"date": (d0 + dt.timedelta(days=i)).isoformat(), "close": p})
    return out


def test_attribution_separates_market_from_company():
    rng = np.random.default_rng(1)
    mkt = rng.normal(0.0005, 0.01, 500)
    idio = rng.normal(0, 0.005, 500)
    stock_r = 1.2 * mkt + idio
    stock_r[400:] -= 0.004  # company-specific bleed after day 400
    m, s = _bars(mkt), _bars(stock_r)
    res = attribute_move(s, m, None, s[400]["date"])
    assert res["available"] and res["dominant_driver"] == "stock_specific"
    assert res["stock_specific_pct"] < -20
    assert 1.0 < res["market_beta"] < 1.4
