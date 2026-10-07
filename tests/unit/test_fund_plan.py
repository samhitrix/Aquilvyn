"""Too many / overlapping funds → a plan a person can actually follow: invest only in one fund per role, hold decent
funds (selling them only creates tax), sell laggards and tiny leftovers in tax-free steps."""
from datetime import date


def _fund(name, value, score=None, regular=False, er=None, lots=(), price=100.0, category="equity:flexi_cap", **kw):
    return {"id": name, "name": name, "value": value, "price": price, "score": score, "regular": regular, "expense_ratio": er,
            "category": category, "lots": [{"buy_date": d, "qty": q, "cost": c} for d, q, c in lots], **kw}


def _item(iid, name):
    return {"subject_key": f"holding:p1:{iid}", "action": "HOLD", "headline": f"Hold {name}", "confidence": 0.5, "input_hash": "h",
            "actionable": False, "priority": 1, "short_report": {"action": "HOLD", "headline": f"Hold {name}", "do": "Do nothing", "reasons": []},
            "full_report": {"summary": {}}}


def test_overlap_plan_holds_decent_funds_and_sells_only_laggards():
    from advisor_svc.engine import _apply_consolidation
    from advisor_svc.health import fund_plan

    old = [("2021-01-01", 1000, 100.0)]
    funds = {"canara": _fund("Canara Large Cap", 300_000, score=78, lots=old, price=300.0),
             "mirae": _fund("Mirae Large Cap", 150_000, score=70, lots=old, price=150.0),            # decent, just redundant
             "quant": _fund("Quant Large Cap", 120_000, score=29, price=120.0, verdict="SWITCH",                # lags, SIP running
                            lots=old + [("2026-08-05", 1, 120.0), ("2026-09-05", 1, 120.0)])}

    def pair(a, b, pct):
        return {"a": funds[a]["name"], "b": funds[b]["name"], "a_id": a, "b_id": b, "overlap_pct": pct, "common_count": 30, "common": [{"name": "ICICI Bank"}]}

    [g] = fund_plan(funds, [pair("canara", "mirae", 73), pair("canara", "quant", 66)], date(2026, 10, 5))
    assert g["keep"] == "Canara Large Cap" and {m["name"]: m["mode"] for m in g["moves"]} == {"Mirae Large Cap": "freeze", "Quant Large Cap": "exit"}
    quant = next(m for m in g["moves"] if m["name"] == "Quant Large Cap")
    assert "tax ₹0" in quant["do"] and "Stop the SIP in Quant Large Cap" in quant["do"]  # ₹20,000 gain within the ₹1.25 lakh
    mirae = next(m for m in g["moves"] if m["name"] == "Mirae Large Cap")
    assert not mirae["sip"] and mirae["do"].startswith("Nothing to do — keep the units")  # an old fund: no SIP to stop

    items = [_item("canara", "Canara Large Cap"), _item("mirae", "Mirae Large Cap"), _item("quant", "Quant Large Cap")]
    _apply_consolidation(items, {"p1": [g]}, "v1")
    keep, mirae, quant_card = items
    assert mirae["action"] == "HOLD" and mirae["short_report"]["verdict"] == "Hold" and "Nothing to do" in mirae["short_report"]["do"]
    assert mirae["short_report"]["sip_into"] == {"name": "Canara Large Cap", "code": None, "active_sip": False}
    assert quant_card["action"] == "SWITCH" and quant_card["short_report"]["switch_into"]["name"] == "Canara Large Cap"
    assert keep["short_report"]["consolidate_from"] == ["Mirae Large Cap", "Quant Large Cap"]


def test_many_funds_become_one_per_role_with_a_phased_tax_aware_plan():
    from advisor_svc.health import fund_plan, fund_role, lookthrough_findings

    gain = [("2020-01-01", 1000, 10.0)]  # ₹90,000 long-term gain at price 100

    def f(name, score, value=100_000.0, **kw):
        return _fund(name, value, score=score, lots=gain, price=100.0, **kw)

    funds = {
        "l1": f("Canara Robeco Large Cap Fund", 80), "l2": f("Axis Large Cap Fund", 30), "l3": f("Nippon India ETF Nifty 50 BeES", None, asset_type="etf"),
        "f1": f("Parag Parikh Flexi Cap Fund", 85), "f2": f("Quant Multi Cap Fund", 29), "f3": f("Axis ELSS Tax Saver Fund", 60, category="equity:elss"),
        "f4": f("Kotak Infrastructure and Economic Reform Fund", 50), "m1": f("Kotak Mid Cap Fund", 75), "m2": f("Axis Midcap Fund", 46),
        "s1": f("SBI Small Cap Fund", 70), "d1": f("HDFC Banking and PSU Debt Fund", None), "d2": f("Edelweiss Gilt Fund", None),
        "tiny": f("Bank of India ELSS Tax Saver Fund", 55, value=4_000.0, category="equity:elss"),
    }
    assert [fund_role(funds[k]) for k in ("l3", "f4", "m2", "d2")] == ["large", "flexi", "mid", "debt"]
    plan = fund_plan(funds, [], date(2026, 10, 5))
    assert {g["role_key"]: g["keep"] for g in plan} == {
        "large": "Canara Robeco Large Cap Fund", "flexi": "Parag Parikh Flexi Cap Fund", "mid": "Kotak Mid Cap Fund",
        "small": "SBI Small Cap Fund", "debt": "HDFC Banking and PSU Debt Fund"}
    moves = {m["name"]: m for g in plan for m in g["moves"]}
    assert moves["Axis Large Cap Fund"]["mode"] == "exit" and moves["Quant Multi Cap Fund"]["mode"] == "exit"  # laggards
    assert moves["Bank of India ELSS Tax Saver Fund"]["mode"] == "exit"                                       # tiny leftover
    for decent in ("Axis ELSS Tax Saver Fund", "Kotak Infrastructure and Economic Reform Fund", "Axis Midcap Fund", "Nippon India ETF Nifty 50 BeES"):
        assert moves[decent]["mode"] == "freeze", decent
    # ₹1.25 lakh tax-free long-term gains a year: the worst laggard goes first; the next is split across financial years
    assert "tax ₹0" in moves["Quant Multi Cap Fund"]["do"] and "After 1 April" in moves["Axis Large Cap Fund"]["do"]
    assert all("Stop the SIP" not in m["do"] for m in moves.values())
    lineup = next(x for x in lookthrough_findings({"risk_profile": "moderate"}, 1.0, None, funds, plan=plan) if x["rule_id"] == "fund_lineup")
    assert lineup["headline"] == "12 mutual funds + 1 ETF — invest only in 5 from now on (one per role); no need to sell them all"
    assert lineup["reasons"][0].startswith("Large cap / index (core) (3): keep Canara Robeco Large Cap Fund; also held: ")
    assert lineup["do"].startswith("Step 1 (now, no tax): From now on put new money only into Canara Robeco Large Cap Fund")
    assert "Step 3: just hold the other" in lineup["do"] and "SIP" not in lineup["do"].split("Step 2")[0]  # old funds: no SIPs to move


def test_a_fund_at_a_loss_is_switched_now_and_says_the_loss_is_booked():
    from advisor_svc.health import fund_plan

    funds = {"uti": _fund("UTI Nifty 50 Index Fund", 300_000, score=70, lots=[("2021-01-01", 1000, 100.0)], price=300.0),
             "hdfc": _fund("HDFC BSE Sensex Index Fund", 100_000, score=30, lots=[("2022-01-01", 1000, 107.0)], price=100.0, verdict="SWITCH")}
    pair = {"a": "UTI Nifty 50 Index Fund", "b": "HDFC BSE Sensex Index Fund", "a_id": "uti", "b_id": "hdfc", "overlap_pct": 83.0,
            "common_count": 30, "common": []}
    [g] = fund_plan(funds, [pair], date(2026, 10, 5))
    m = g["moves"][0]
    assert m["mode"] == "exit" and "at a loss, so no tax" in m["do"] and "₹7,000 loss is booked" in m["do"]
    assert m["costs"][0].startswith("Selling also books a ₹7,000 loss")


def test_active_sip_is_read_from_the_cas_purchase_dates():
    from advisor_svc.health import sip_active

    def lots(*days):
        return [{"buy_date": d, "qty": 50, "cost": 160.0} for d in days]

    today = date(2026, 10, 5)
    monthly = lots("2026-04-06", "2026-05-04", "2026-06-02", "2026-07-02", "2026-08-03")  # last instalment 63 days ago (one late)
    assert sip_active(monthly, today)
    assert sip_active(lots("2023-01-10", "2026-09-04"), today)                     # bought last month
    assert not sip_active(lots("2021-01-01", "2022-06-01"), today)                 # an old fund
    assert not sip_active(lots("2026-01-05", "2026-02-05", "2026-03-05"), today)   # SIP stopped in March
    assert not sip_active(lots("2026-08-03"), today)                               # one lump sum two months ago
    assert not sip_active([{"buy_date": "2026-10-05", "qty": 1, "cost": 1}][:0], today)
