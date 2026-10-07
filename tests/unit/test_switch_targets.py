"""A SWITCH names where to go: the best-scored funds in the same AMFI category right now."""


def test_peers_are_other_direct_growth_funds_of_the_same_category():
    from market_svc.fund_holdings import category_label, fund_peers

    cat = "Open Ended Schemes(Equity Scheme - Multi Cap Fund)"
    by_code = {
        "1": {"name": "Quant Multi Cap Fund - Direct Plan - Growth Option", "category": cat, "amc": "Quant Mutual Fund"},
        "2": {"name": "Quant Multi Cap Fund - Regular Plan - Growth", "category": cat},                     # same fund
        "3": {"name": "Nippon India Multi Cap Fund - Direct Plan Growth Plan - Growth Option", "category": cat},
        "4": {"name": "Nippon India Multi Cap Fund - Direct Plan - IDCW Option", "category": cat},           # not growth
        "5": {"name": "HDFC Multi Cap Fund - Regular Plan - Growth", "category": cat},                       # not direct
        "6": {"name": "Kotak Small Cap Fund - Direct Plan - Growth", "category": "Open Ended Schemes(Equity Scheme - Small Cap Fund)"},
    }
    res = fund_peers("1", by_code)
    assert category_label(cat) == "Multi Cap Fund" and res["label"] == "Multi Cap Fund"
    assert [p["code"] for p in res["peers"]] == ["3"]


def test_category_ranking_orders_by_score_then_consistency():
    from analytics_svc.api import peer_row, rank

    def a(score, cons, r3=None):
        return {"fund": {"available": True, "score": score, "consistency_pct": cons, "alpha_pct": 1.0,
                         "rolling": {"3y": {"fund_avg_pct": r3}} if r3 else {}, "risk": {"cagr_pct": 15.0}}}

    rows = rank([peer_row("1", "A", a(70, 60, 14.0)), peer_row("2", "B", a(82, 88, 18.2)), peer_row("3", "C", a(70, 75)),
                 peer_row("4", "D", {"fund": {"available": False}})])
    assert [r["code"] for r in rows] == ["2", "3", "1"] and rows[0]["return_3y_pct"] == 18.2 and rows[1]["rolling_years"] == 1


def test_a_switch_card_names_the_best_funds_and_prefers_one_you_hold():
    from advisor_svc.engine import add_switch_targets

    it = {"symbol": "1", "name": "Quant Multi Cap Fund", "input_hash": "h",
          "short_report": {"do": "Selling now: est. tax ₹1,737.", "reasons": ["Lagged its benchmark"]}}
    res = {"label": "Multi Cap Fund", "self": {"score": 29.0}, "self_rank": 27, "of": 31, "ranking": [
        {"code": "3", "name": "Nippon India Multi Cap", "score": 84.0, "consistency_pct": 88.0, "return_3y_pct": 18.2, "rolling_years": 3},
        {"code": "7", "name": "Kotak Multicap", "score": 80.0, "consistency_pct": 81.0, "return_3y_pct": 17.0, "rolling_years": 3},
        {"code": "8", "name": "HDFC Multi Cap", "score": 76.0, "consistency_pct": 70.0, "return_3y_pct": 16.1, "rolling_years": 3},
        {"code": "9", "name": "ICICI Multicap", "score": 74.0, "consistency_pct": 66.0, "return_3y_pct": 15.0, "rolling_years": 3}]}
    add_switch_targets(it, res, {"9"}, "v1")
    sr = it["short_report"]
    assert [c["code"] for c in sr["switch_candidates"]] == ["9", "3", "7"] and sr["switch_candidates"][0]["held"]
    assert sr["do"].startswith("Switch into one of the best Multi Cap Fund funds right now: 1) ICICI Multicap — you already hold it")
    assert sr["reasons"][0].startswith("Quant Multi Cap Fund ranks 27 of 31 Multi Cap Fund funds") and sr["do"].endswith("est. tax ₹1,737.")
