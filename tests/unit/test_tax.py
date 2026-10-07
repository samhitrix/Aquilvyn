from datetime import date

from advisor_svc import taxrules


def row(lots, price=200.0, at="stock"):
    return {"asset_type": at, "price": price, "lots": lots}


def test_rates_switch_on_23_july_2024():
    assert taxrules.rates(date(2024, 7, 22))["stcg"] == 15.0
    assert taxrules.rates(date(2024, 7, 23)) == {"stcg": 20.0, "ltcg": 12.5, "ltcg_exemption": 125000.0}


def test_wait_for_ltcg_saving():
    today = date(2026, 9, 1)
    r = taxrules.timing(row([{"buy_date": "2025-09-20", "qty": 100, "cost": 100.0}]), {}, 30, today)
    assert r["days_to_ltcg"] == 20  # LT on 2025-09-20 + 366 days
    assert r["saving_if_wait"] == 10000 * (20 - 12.5) / 100


def test_debt_mf_after_april_2023_is_slab_only():
    r = taxrules.timing(row([{"buy_date": "2023-06-01", "qty": 10, "cost": 10.0}], 12.0, "mutual_fund"), {"mf_category": "debt:corporate_bond"}, 30, date(2026, 9, 1))
    assert r["equity_taxation"] is False and "days_to_ltcg" not in r and r["lots"][0]["term"] == "slab"
