"""True stock exposure through funds, and fund overlap (E16)."""
import pytest

from fm_common.lookthrough import combine, overlap

HDFCB, ICICIB, RIL, TCS, GSEC = "INE040A01034", "INE090A01021", "INE002A01018", "INE467B01029", "IN0020230085"


def fund(*rows, as_of="2026-07-31", stale=False):
    return {"available": True, "scheme": "x", "as_of": as_of, "stale": stale,
            "holdings": [{"isin": i, "name": n, "kind": k, "weight": w} for i, n, k, w in rows]}


FUND_A = fund((HDFCB, "HDFC Bank", "equity", 10.0), (ICICIB, "ICICI Bank", "equity", 8.0), (GSEC, "GOI 2033", "debt", 5.0))
FUND_B = fund((HDFCB, "HDFC Bank", "equity", 9.0), (RIL, "Reliance", "equity", 7.0), (TCS, "TCS", "equity", 4.0))


def test_direct_plus_via_funds_is_the_true_exposure():
    positions = [
        {"instrument_id": "s1", "name": "HDFC Bank", "value": 50_000, "asset_type": "stock"},
        {"instrument_id": "fa", "name": "Fund A", "value": 200_000, "asset_type": "mutual_fund"},
        {"instrument_id": "fb", "name": "Fund B", "value": 100_000, "asset_type": "mutual_fund"},
        {"instrument_id": "fc", "name": "Fund C (no data yet)", "value": 80_000, "asset_type": "mutual_fund"},
        {"instrument_id": "epf", "name": "EPF", "value": 900_000, "asset_type": "epf"},
    ]
    r = combine(positions, {"fa": FUND_A, "fb": FUND_B, "fc": {"available": False}}, {"s1": HDFCB})
    hdfc = r["stocks"][0]
    assert hdfc["isin"] == HDFCB
    assert hdfc["direct"] == 50_000 and hdfc["via_funds"] == pytest.approx(20_000 + 9_000)
    assert hdfc["total"] == pytest.approx(79_000)
    assert [f["name"] for f in hdfc["funds"]] == ["Fund A", "Fund B"]
    assert all(s["isin"] != GSEC for s in r["stocks"])  # bonds inside a fund are not stock exposure
    assert r["missing"] == ["Fund C (no data yet)"]
    assert r["fund_value"] == 300_000


def test_overlap_counts_shared_stocks_on_normalised_equity_weights():
    pairs = overlap([{"name": "A", **FUND_A}, {"name": "B", **FUND_B}])
    assert len(pairs) == 1
    # A equity: HDFC 10/18=55.6, ICICI 44.4 · B: HDFC 9/20=45, RIL 35, TCS 20 → overlap = min(55.6, 45) = 45
    assert pairs[0]["overlap_pct"] == pytest.approx(45.0)
    assert pairs[0]["common"][0]["name"] == "HDFC Bank" and pairs[0]["common_count"] == 1
    same = overlap([{"name": "A", **FUND_A}, {"name": "A again", **FUND_A}])
    assert same[0]["overlap_pct"] == pytest.approx(100.0)
    assert overlap([{"name": "A", **FUND_A}, {"name": "B", **FUND_B}], min_pct=50) == []


def test_advisor_flags_hidden_concentration_and_redundant_funds():
    from advisor_svc.health import findings

    positions = [
        {"instrument_id": "s1", "name": "HDFC Bank", "value": 70_000, "asset_type": "stock"},  # 7% direct: under a 10% cap
        {"instrument_id": "fa", "name": "Fund A", "value": 300_000, "asset_type": "mutual_fund"},
        {"instrument_id": "fa2", "name": "Fund A twin", "value": 100_000, "asset_type": "mutual_fund"},
    ]
    look = combine(positions, {"fa": FUND_A, "fa2": FUND_A}, {"s1": HDFCB})
    look["overlap"] = overlap([{"name": "Fund A", **FUND_A}, {"name": "Fund A twin", **FUND_A}], min_pct=50)
    rows = [{"asset_type": "stock", "market_value": 70_000, "quantity": 1, "meta": {}, "name": "HDFC Bank"},
            {"asset_type": "mutual_fund", "market_value": 400_000, "quantity": 1, "meta": {"mf_category": "equity:flexi_cap"}, "name": "Fund A"},
            {"asset_type": "epf", "market_value": 530_000, "quantity": 1, "meta": {}, "name": "EPF"}]
    summary = {"market_value": 1_000_000, "allocation": {"equity": {"pct": 47}, "debt": {"pct": 53}}}
    profile = {"risk_profile": "moderate", "age": 40, "target_allocation": {"equity": 47, "debt": 53}}
    out = {f["rule_id"].split(":")[0]: f for f in findings(profile, summary, rows, look)}
    hidden = out["hidden_concentration"]
    # HDFC Bank: 70k direct + 10% of 400k via funds = 110k = 11% > 10% cap, though only 7% directly
    assert hidden["action"] == "REBALANCE" and "11%" in hidden["headline"] and "Fund A" in hidden["reasons"][0]
    assert out["fund_overlap"]["headline"].endswith("(100% overlap)")
    assert all(f["action"] != "REVIEW" for f in out.values())  # REVIEW now means "data missing" on holdings


def test_a_stock_entered_without_isin_is_matched_by_company_name():
    from fm_common.lookthrough import company_key

    assert company_key("HDFC Bank Ltd.") == company_key("HDFC BANK LIMITED") == company_key("HDFC Bank") == "hdfc bank"
    assert company_key("Larsen & Toubro Ltd.") == "larsen and toubro"
    r = combine([{"instrument_id": "s1", "name": "HDFC Bank Limited", "value": 10_000, "asset_type": "stock"},
                 {"instrument_id": "fa", "name": "Fund A", "value": 100_000, "asset_type": "mutual_fund"}], {"fa": FUND_A}, {})
    hdfc = next(s for s in r["stocks"] if s["isin"] == HDFCB)
    assert hdfc["direct"] == 10_000 and hdfc["via_funds"] == 10_000


def test_funds_without_stocks_are_neither_exposure_nor_missing():
    r = combine([{"instrument_id": "fa", "name": "Fund A", "value": 100_000, "asset_type": "mutual_fund"},
                 {"instrument_id": "liq", "name": "Liquid BeES", "value": 50_000, "asset_type": "etf"}],
                {"fa": FUND_A, "liq": {"available": False, "no_stocks": True}}, {})
    assert r["missing"] == [] and r["fund_value"] == 100_000


def test_holdings_known_only_by_name_line_up_with_isins_from_other_sources():
    by_name = {"available": True, "scheme": "g", "holdings": [
        {"isin": "NAME:hdfc bank", "name": "HDFC Bank Ltd", "kind": "equity", "weight": 10.0},
        {"isin": "NAME:tcs", "name": "TCS", "kind": "equity", "weight": 5.0}]}
    r = combine([{"instrument_id": "s1", "name": "HDFC Bank Limited", "value": 20_000, "asset_type": "stock"},
                 {"instrument_id": "fa", "name": "Fund A", "value": 100_000, "asset_type": "mutual_fund"},
                 {"instrument_id": "fg", "name": "Fund G", "value": 100_000, "asset_type": "mutual_fund"}],
                {"fa": FUND_A, "fg": by_name}, {"s1": HDFCB})
    hdfc = next(s for s in r["stocks"] if s["isin"] == HDFCB)
    assert hdfc["direct"] == 20_000 and hdfc["via_funds"] == 20_000 and len(hdfc["funds"]) == 2
    assert overlap([{"name": "A", **FUND_A}, {"name": "G", **by_name}])[0]["common_count"] == 1


def _fund(name, value, score=None, regular=False, er=None, lots=(), price=100.0, category="equity:flexi_cap"):
    return {"id": name, "name": name, "value": value, "price": price, "score": score, "regular": regular, "expense_ratio": er,
            "category": category, "lots": [{"buy_date": d, "qty": q, "cost": c} for d, q, c in lots]}


PAIR = {"a": "Fund A", "b": "Fund B", "a_id": "Fund A", "b_id": "Fund B", "overlap_pct": 72.0, "common_count": 30,
        "common": [{"name": "HDFC Bank", "a_weight": 9, "b_weight": 8}]}


def test_overlap_keeps_the_better_fund_and_says_how_to_move_out_cheaply():
    from datetime import date

    from advisor_svc.health import overlap_advice

    today = date(2026, 10, 4)
    a = _fund("Fund A", 200_000, score=72, lots=[("2023-01-10", 1000, 60.0), ("2026-08-01", 1000, 95.0)])
    b = _fund("Fund B", 100_000, score=55, lots=[("2022-05-01", 1000, 50.0)])
    adv = overlap_advice(PAIR, a, b, today)
    assert adv["keep"] == "Fund A" and adv["exit"] == "Fund B" and "72/100 vs 55/100" in adv["why_keep"]
    # all of B is over a year old: long-term tax only (12.5 % of ₹50,000 gain) — cheap enough? 6,250 on 1,00,000 is not
    assert adv["exit_cost"]["tax"] == 6250 and adv["exit_cost"]["exit_load"] == 0
    assert adv["do"].startswith("Move your SIP from Fund B to Fund A") and "Units held over a year (₹1.00 L)" in adv["do"]


def test_overlap_prefers_direct_plan_then_cost_then_bigger_holding():
    from datetime import date

    from advisor_svc.health import overlap_advice

    t = date(2026, 10, 4)
    assert overlap_advice(PAIR, _fund("Fund A", 9, regular=True), _fund("Fund B", 1), t)["keep"] == "Fund B"
    assert overlap_advice(PAIR, _fund("Fund A", 1, er=0.4), _fund("Fund B", 9, er=0.9), t)["keep"] == "Fund A"
    assert overlap_advice(PAIR, _fund("Fund A", 1), _fund("Fund B", 9), t)["keep"] == "Fund B"


def test_recent_units_wait_a_year_and_elss_units_wait_for_their_lock_in():
    from datetime import date

    from advisor_svc.health import exit_cost, overlap_advice

    t = date(2026, 10, 4)
    young = _fund("Fund B", 100_000, lots=[("2026-06-01", 1000, 90.0)])
    c = exit_cost(young, t)
    assert c["short_value"] == 100_000 and c["exit_load"] == 1000 and c["tax"] == 2000  # 20 % of ₹10,000 short-term gain
    adv = overlap_advice(PAIR, _fund("Fund A", 500_000, score=80), young, t)
    assert "as each instalment turns 1 year old" in adv["do"]
    elss = _fund("Fund B Tax Saver", 50_000, category="equity:elss", lots=[("2025-02-01", 500, 80.0)])
    adv = overlap_advice({**PAIR, "b": "Fund B Tax Saver"}, _fund("Fund A", 500_000, score=80), elss, t)
    assert adv["move_value"] == 0 and "locked (ELSS)" in adv["do"] and "2028-02-01" in adv["do"]


def test_a_fund_the_advisor_says_to_sell_is_never_the_one_to_keep():
    from datetime import date

    from advisor_svc.health import consolidation_plan

    funds = {"a": {**_fund("Fund A", 500_000, score=90), "verdict": "TRIM"}, "b": _fund("Fund B", 100_000, score=60)}
    pair = {"a": "Fund A", "b": "Fund B", "a_id": "a", "b_id": "b", "overlap_pct": 80.0, "common_count": 30, "common": []}
    [g] = consolidation_plan(funds, [pair], date(2026, 10, 5))
    assert g["keep"] == "Fund B" and g["moves"][0]["name"] == "Fund A"
