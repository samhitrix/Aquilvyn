from datetime import date
from decimal import Decimal as D

import pytest

from portfolio_svc.ledger import LedgerError, Txn, accrued_value, build_positions, xirr


def t(i, typ, d, q, p, amount=0, sym="ABC.NS", at="stock"):
    return Txn(str(i), "inst", sym, at, typ, date.fromisoformat(d), D(str(q)), D(str(p)), D(0), D(str(amount)))


def test_fifo_sell_consumes_oldest_lot_first():
    pos = build_positions([t(1, "buy", "2023-01-01", 10, 100), t(2, "buy", "2024-01-01", 10, 200), t(3, "sell", "2024-06-01", 15, 300)])["inst"]
    assert pos.qty == 5 and pos.lots[0].cost_per_unit == 200
    gains = [(r.qty, r.gain) for r in pos.realised]
    assert gains == [(D(10), D(2000)), (D(5), D(500))]


def test_split_restates_lots_and_keeps_holding_period():
    pos = build_positions([t(1, "buy", "2023-01-01", 10, 1000), t(2, "split", "2024-01-01", 5, 0)])["inst"]
    assert pos.qty == 50 and pos.lots[0].cost_per_unit == 200 and pos.lots[0].buy_date == date(2023, 1, 1)


def test_bonus_adds_zero_cost_units():
    pos = build_positions([t(1, "buy", "2023-01-01", 10, 100), t(2, "bonus", "2024-01-01", 10, 0)])["inst"]
    assert pos.qty == 20 and pos.invested == 1000 and pos.avg_cost == 50


def test_strict_mode_rejects_overselling():
    with pytest.raises(LedgerError):
        build_positions([t(1, "buy", "2023-01-01", 1, 100), t(2, "sell", "2023-02-01", 2, 100)], strict=True)


def test_epf_accrual_uses_rate_and_never_double_counts_interest():
    txns = [t(1, "contribution", "2023-01-01", 0, 0, 100000, "EPF-1", "epf")]
    pos = build_positions(txns)["inst"]
    v = accrued_value(pos, 8.25, date(2024, 1, 1))
    assert v == pytest.approx(D("108250"), rel=D("0.001"))
    txns.append(t(2, "interest", "2024-01-01", 0, 0, 8250, "EPF-1", "epf"))
    pos = build_positions(txns)["inst"]
    assert accrued_value(pos, 8.25, date(2024, 1, 1)) == D("108250.00")  # interest already credited
    assert pos.invested == 100000  # interest is gain, not invested capital


def test_xirr_simple_doubling():
    r = xirr([(date(2020, 1, 1), -100.0), (date(2021, 1, 1), 110.0)])
    assert r == pytest.approx(0.10, abs=1e-3)


def test_xirr_none_when_no_sign_change():
    assert xirr([(date(2020, 1, 1), -100.0), (date(2021, 1, 1), -10.0)]) is None


def test_xirr_never_raises_on_extreme_gains():
    # a 1000x gain over many years overflowed float math and 500'd /holdings
    flows = [(date(2015, 1, 1), -100.0), (date(2016, 6, 1), -50.0), (date(2026, 1, 1), 1e8)]
    r = xirr(flows)
    assert r is None or r > 0


def test_xirr_under_a_year_is_not_annualised():
    flows = [(date(2026, 1, 1), -100.0), (date(2026, 3, 1), 150.0)]
    assert xirr(flows, min_days=365) is None
    assert xirr(flows) is not None
