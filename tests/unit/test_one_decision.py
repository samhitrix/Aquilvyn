"""One decision per fund: the AI review comments on a fund-plan decision but never rewrites or overrules it,
and the switch targets on a rule-based SWITCH survive the review."""
from types import SimpleNamespace

from advisor_svc.ai.base import Verdict
from advisor_svc.ai.review import apply_ai_to_verdict


def _rec(sr, action="SWITCH"):
    return SimpleNamespace(scope="holding", short_report=sr, ai_consensus="needs_review", confidence=0.8, name="HDFC BSE Sensex Index Fund",
                           symbol="119065", action=action, headline=sr["headline"], asset_type="mutual_fund", bucket="focus", actionable=True)


def test_ai_disagreement_is_a_comment_on_a_fund_plan_decision():
    sr = {"decided_by": "fund_plan", "action": "SWITCH", "rule_action": "SWITCH", "verdict": "Switch",
          "headline": "Switch HDFC BSE Sensex Index Fund → UTI Nifty 50 Index Fund", "do": "Sell over time — it lags …",
          "switch_into": {"name": "UTI Nifty 50 Index Fund"}}
    rec = _rec(dict(sr))
    apply_ai_to_verdict(rec, [Verdict("groq", "m", "disagree", confidence=0.9, suggested_action="HOLD", plain_verdict="Hold it")])
    out = rec.short_report
    assert rec.action == "SWITCH" and out["do"] == sr["do"] and out["switch_into"] == sr["switch_into"]
    assert out["ai_suggests"]["applied"] is False and "fund plan" in out["ai_suggests"]["why_not"]
    assert out["ai_view"]["stance"] == "disagree"


def test_switch_targets_are_kept_through_a_review():
    sr = {"action": "SWITCH", "rule_action": "SWITCH", "verdict": "Switch", "headline": "Switch Quant Multi Cap Fund", "rules_confidence": 0.8,
          "do_prefix": "Switch into one of the best Multi Cap Fund funds right now: 1) Nippon India Multi Cap …",
          "do": "…", "rule_suggestion": {"type": "switch"}, "asset_type": "mutual_fund"}
    rec = _rec(dict(sr))
    rec.ai_consensus = "agree"
    apply_ai_to_verdict(rec, [Verdict("groq", "m", "agree", confidence=0.8, suggested_action="SWITCH", plain_verdict="Switch")])
    assert rec.action == "SWITCH" and rec.short_report["do"].startswith(sr["do_prefix"])


# ---- one method, one verdict per fund (agreed design) -------------------------------------------------------------
from datetime import date  # noqa: E402

TODAY = date(2026, 10, 5)


def _fund(name, value, score=None, lots=(("2020-01-01", 1000, 10.0),), price=100.0, category="equity:flexi_cap", **kw):
    return {"id": name, "name": name, "value": value, "price": price, "score": score, "regular": False, "expense_ratio": None,
            "category": category, "lots": [{"buy_date": d, "qty": q, "cost": c} for d, q, c in lots], **kw}


def _item(key, f):
    return {"subject_key": f"holding:p1:{key}", "action": "HOLD", "headline": f"Hold {f['name']}", "confidence": 0.5, "input_hash": "h",
            "actionable": False, "priority": 1, "short_report": {"action": "HOLD", "headline": f"Hold {f['name']}", "do": "Do nothing", "reasons": []},
            "full_report": {"summary": {}}}


def test_few_funds_use_the_same_role_lineup_and_never_merge_across_roles():
    """An index fund and a flexi-cap fund overlap a lot, but they do different jobs: both stay (no fund-count switch-over)."""
    from advisor_svc.health import fund_plan, lookthrough_findings

    funds = {"idx": _fund("UTI Nifty 50 Index Fund", 200_000, 70, category="equity:index"), "flexi": _fund("Parag Parikh Flexi Cap Fund", 200_000, 80)}
    pair = {"a": "UTI Nifty 50 Index Fund", "b": "Parag Parikh Flexi Cap Fund", "a_id": "idx", "b_id": "flexi", "overlap_pct": 62.0, "common_count": 20, "common": []}
    plan = fund_plan(funds, [pair], TODAY)
    assert {g["role_key"]: (g["keep"], len(g["moves"])) for g in plan} == {"large": ("UTI Nifty 50 Index Fund", 0), "flexi": ("Parag Parikh Flexi Cap Fund", 0)}
    assert not [f for f in lookthrough_findings({"risk_profile": "moderate"}, 1.0, None, funds, plan=plan) if f["rule_id"].startswith("fund_")]


def test_a_lagging_keeper_is_replaced_never_called_invest_here():
    from advisor_svc.engine import _apply_consolidation
    from advisor_svc.health import fund_lineup, lookthrough_findings, phase

    funds = {"sbi": _fund("SBI Small Cap Fund", 300_000, 40, verdict="SWITCH"), "axis": _fund("Axis Small Cap Fund", 100_000, 30, verdict="SWITCH"),
             "ppfas": _fund("Parag Parikh Flexi Cap Fund", 300_000, 80)}
    plan = fund_lineup(funds, [], TODAY)
    small = next(g for g in plan if g["role_key"] == "small")
    assert small["replace"] and small["keep"] == "SBI Small Cap Fund"
    small["into_name"], small["into_code"] = "Bandhan Small Cap Fund", "147946"  # what the engine's market re-target finds
    phase(plan, funds, TODAY)
    axis = small["moves"][0]
    assert axis["mode"] == "exit" and "Bandhan Small Cap Fund" in axis["do"] and "SBI Small Cap" not in axis["do"]
    assert small["keep_move"]["mode"] == "exit" and "Bandhan Small Cap Fund" in small["keep_move"]["do"]
    lineup = next(f for f in lookthrough_findings({"risk_profile": "moderate"}, 1.0, None, funds, plan=plan) if f["rule_id"] == "fund_lineup")
    assert any(r.startswith("Small cap (2): replace SBI Small Cap Fund (it lags) with Bandhan Small Cap Fund") for r in lineup["reasons"])
    assert "Bandhan Small Cap Fund" in lineup["do"] and "into SBI" not in lineup["do"]

    items = [_item(k, funds[k]) for k in ("sbi", "axis", "ppfas")]
    _apply_consolidation(items, {"p1": plan}, "v1", TODAY)
    sbi, axis_card, ppfas = items
    assert sbi["action"] == "SWITCH" and sbi["short_report"]["switch_into"]["name"] == "Bandhan Small Cap Fund"
    assert axis_card["action"] == "SWITCH" and axis_card["short_report"]["switch_into"]["name"] == "Bandhan Small Cap Fund"
    assert ppfas["action"] == "ADD" and ppfas["short_report"]["verdict"] == "Keep · invest here"   # the fund to keep says so
    assert {i["short_report"]["decided_by"] for i in items} == {"fund_plan"}


def test_plan_phases_within_the_tax_pages_headroom_and_records_what_it_uses():
    from advisor_svc.health import fund_plan

    funds = {"keep": _fund("Kotak Mid Cap Fund", 300_000, 75), "lag": _fund("Axis Midcap Fund", 100_000, 30)}  # ₹90,000 long-term gain
    full = fund_plan(funds, [], TODAY)[0]["moves"][0]
    assert full["fy_ltcg"] == 90_000 and "tax ₹0" in full["do"]
    part = fund_plan(funds, [], TODAY, ltcg_left=45_000)[0]["moves"][0]  # ₹80,000 already used elsewhere this year
    assert part["fy_ltcg"] == 45_000 and "sell about 50%" in part["do"] and "After 1 April" in part["do"]
    none = fund_plan(funds, [], TODAY, ltcg_left=0)[0]["moves"][0]
    assert none["fy_ltcg"] == 0 and none["do"].count("After 1 April") == 1


def _h(iid, name, lots, price=100.0, at="mutual_fund", cat="equity:flexi_cap"):
    return {"instrument_id": iid, "profile_id": "p1", "symbol": iid, "name": name, "asset_type": at, "price": price,
            "meta": {"mf_category": cat}, "lots": [{"buy_date": d, "qty": q, "cost": c} for d, q, c in lots]}


def _summary(left=125_000.0):
    return {"slab_pct": 30.0, "lines": [{"key": "st_equity", "taxable": 50_000.0}], "ltcg_exemption": {"left": left}}


def test_tax_page_takes_the_plans_gain_off_the_allowance_once():
    from portfolio_svc.tax import harvest

    rows = [_h("lag", "Axis Midcap Fund", [("2020-01-01", 1000, 10.0)]), _h("keep", "Kotak Mid Cap Fund", [("2020-01-01", 1000, 50.0)])]
    h = harvest("2026-27", _summary(), rows, TODAY, planned={"lag": 90_000.0})
    assert h["plan_ltcg"] == 90_000 and h["room_left"] <= 35_000
    gains = [a for a in h["actions"] if a["kind"] == "gain"]
    assert [a["instrument_id"] for a in gains] == ["keep"] and gains[0]["booked"] <= 35_000  # never the fund being sold


def test_harvesting_never_suggests_selling_elss_units_still_locked_in():
    from portfolio_svc.tax import harvest

    elss = _h("elss", "Axis ELSS Tax Saver Fund", [("2020-01-01", 100, 50.0), ("2025-01-01", 1000, 150.0)], cat="equity:elss")
    h = harvest("2026-27", _summary(), [elss], TODAY)
    loss = [a for a in h["actions"] if a["kind"] == "loss"]
    assert not loss  # the ₹50,000 loss sits in units bought 2025 — locked until 2028
    gain = [a for a in h["actions"] if a["kind"] == "gain"]
    assert gain and gain[0]["quantity"] == 100  # only the free (old) units


def test_an_index_fund_is_never_a_laggard_for_its_score():
    """Same rule as the rulebook: an index fund tracks the index by design, so a low 'beat the benchmark' score
    doesn't make the role 'replace' — the active large-cap fund that overlaps it moves into it."""
    from advisor_svc.health import fund_lineup, lags

    uti = _fund("UTI Nifty 50 Index Fund - Direct Growth", 70_000, 38, category="equity:index")
    mirae = _fund("Mirae Asset Large Cap Fund - Direct Growth", 80_000, 35, category="equity:large_cap")
    assert not lags(uti) and lags(mirae)
    [g] = fund_lineup({"uti": uti, "mirae": mirae}, [], TODAY)
    assert g["keep"] == uti["name"] and not g["replace"] and g["moves"][0]["name"] == mirae["name"]


def test_a_running_sip_in_a_good_fund_is_never_moved_or_called_too_small():
    """The reported case: a new SIP in Parag Parikh (₹982 so far) next to a bigger, older Kotak Large & Mid Cap holding.
    The SIP fund stays the one to invest in; the bigger old fund is held (no new money) — not the other way round."""
    from advisor_svc.health import fund_plan

    recent = [(d, 1, 982.0) for d in ("2026-09-06",)]
    for score in (85, None):  # also when the fund's score hasn't loaded yet: unknown is never "worst"
        funds = {"kotak": _fund("Kotak Large & Mid Cap Fund - Direct Plan - Growth", 300_000, 78, category="equity:large_and_mid_cap"),
                 "ppfas": _fund("Parag Parikh Flexi Cap Fund - Direct Growth", 982, score, lots=recent, price=982.0, sip_active=True)}
        [g] = fund_plan(funds, [], TODAY)
        assert g["keep"] == "Parag Parikh Flexi Cap Fund - Direct Growth", score
        [m] = g["moves"]
        assert m["name"].startswith("Kotak") and m["mode"] == "freeze" and "Parag Parikh" in m["do"]
        assert "your SIP already goes here" in g["keep_reason"]


def test_a_running_sip_in_a_lagging_fund_is_still_moved():
    from advisor_svc.health import fund_plan

    funds = {"good": _fund("Parag Parikh Flexi Cap Fund - Direct Growth", 300_000, 85),
             "lag": _fund("Quant Flexi Cap Fund - Direct Growth", 50_000, 30, sip_active=True, lots=[("2026-09-06", 10, 100.0)])}
    [g] = fund_plan(funds, [], TODAY)
    assert g["keep"].startswith("Parag Parikh") and g["moves"][0]["mode"] == "exit" and "Stop the SIP in Quant" in g["moves"][0]["do"]
