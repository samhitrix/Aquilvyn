"""Golden cases for the Advisor rulebook: these are the calls an experienced investor would make."""
import pytest

from advisor_svc.rules import evaluate, load_rulebook

BASE = {"dq.grade": "A", "asset_type": "stock", "intent": "core", "regime": "bull", "pos.over_cap": False, "dd.triggered": False,
        "fund.score": 60, "tech.score": 50, "fund.red_flag_count": 0, "thesis.failed_count": 0, "trade.stop_hit": False, "trade.target_hit": False,
        "tech.above_200dma": True, "tech.above_20dma": True, "tech.stabilised": True, "mf.is_regular": None,
        "regime.caution": False, "regime.blocks_new_risk": False, "watch.days": 0}


def decide(**kw):
    return evaluate({**BASE, **{k.replace("__", "."): v for k, v in kw.items()}}).rule


def test_rulebook_loads_and_ends_with_default():
    book = load_rulebook()
    assert book.rules[-1].id == "default_hold" and not book.rules[-1].when


@pytest.mark.parametrize("facts,expected", [
    ({"dq__grade": "D"}, "REVIEW"),
    ({"intent": "trade", "trade__stop_hit": True}, "EXIT"),
    ({"dd__triggered": True, "dd__driver": "market", "fund__score": 75}, "ACCUMULATE"),          # market dip, great business
    ({"dd__triggered": True, "dd__driver": "stock_specific", "fund__score": 30, "tech__above_200dma": False}, "EXIT"),  # broken company
    ({"regime": "correction", "fund__score": 70, "pos__over_cap": False, "dd__triggered": False}, "HOLD"),  # don't panic-sell quality
    ({"pos__over_cap": True, "fund__score": 55}, "TRIM"),
    ({"asset_type": "mutual_fund", "mf__is_regular": True}, "SWITCH"),
    ({"asset_type": "epf"}, "HOLD"),
    ({"fund__score": 80, "tech__score": 70}, "ADD"),
    # a Regular-plan fund in a dip: switch to Direct first, don't "accumulate" the costly plan
    ({"asset_type": "mutual_fund", "mf__is_regular": True, "dd__triggered": True, "mf__score": 70}, "SWITCH"),
    # index funds track the benchmark by design — never flagged as laggards
    ({"asset_type": "mutual_fund", "mf__is_regular": False, "mf__is_index": True, "mf__score": 30, "mf__consistency": 20}, "HOLD"),
    ({"asset_type": "mutual_fund", "mf__is_regular": False, "mf__is_index": False, "mf__score": 30, "mf__consistency": 20}, "SWITCH"),
])
def test_golden_calls(facts, expected):
    assert decide(**facts).action == expected


def test_trace_records_why_rules_failed():
    d = evaluate({**BASE})
    first = d.trace[0]
    assert first["rule"] == "data_insufficient" and not first["matched"] and first["failed_condition"]["actual"] == "A"


def test_missing_trend_data_never_counts_as_below_200dma():
    # 0 daily bars → above_200dma unknown: "below its 200-DMA — the setup has failed" must not fire
    unknown = decide(intent="trade", dd__triggered=True, tech__above_20dma=None)
    assert unknown.id != "trade_setup_failed"
    assert decide(intent="trade", dd__triggered=True, tech__above_20dma=False).id == "trade_setup_failed"
    broken = decide(dd__triggered=True, dd__driver="stock_specific", fund__score=30, tech__above_200dma=None)
    assert broken.id != "broken_company"


def test_no_history_and_no_fundamentals_is_grade_d():
    from datetime import UTC, datetime

    from market_svc.quality import assess

    now = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    dq = assess("stock", {"ts": now.isoformat(), "source": "yahoo"}, [], None, now=now)
    assert dq["grade"] == "D"  # → the data_insufficient guard answers REVIEW instead of EXIT/ADD
    assert decide(dq__grade=dq["grade"], intent="trade", dd__triggered=True).action == "REVIEW"


def test_verdict_is_plain_and_honest_about_confidence():
    from advisor_svc.report import verdict_for

    sell = {"type": "sell", "quantity": 105, "value": 40_000, "note": "Sell the full position"}
    hi = verdict_for("EXIT", 0.82, "CANBK", "Sell the full position", sugg=sell, price=381)
    assert (hi["action"], hi["label"], hi["conviction"], hi["headline"]) == ("EXIT", "Sell", "high", "Sell CANBK")
    assert hi["do"] == "Sell all 105 shares (≈ ₹40,000)."
    mid = verdict_for("EXIT", 0.68, "CANBK", "Sell the full position", sugg=sell, price=381)  # medium: a smaller, decisive step
    assert mid["action"] == "EXIT" and mid["conviction"] == "medium" and mid["headline"] == "Sell half of CANBK"
    assert mid["do"].startswith("Sell half now — 52 shares")
    low = verdict_for("EXIT", 0.5, "CANBK", "Sell the full position", sugg=sell, price=381)
    assert (low["action"], low["label"], low["leaning"], low["headline"]) == ("HOLD", "Hold", "EXIT", "Hold CANBK")
    assert low["do"].startswith("Do nothing — keep holding.")
    buy = verdict_for("ACCUMULATE", 0.7, "HDFC Bank", None, sugg={"type": "buy", "value": 30_000, "tranches": 3}, price=1_500)
    assert buy["do"] == "Buy ≈ ₹15,000 (~10 shares). Start small — the signal is moderate."
    assert verdict_for("HOLD", 0.4, "ITC", None)["action"] == "HOLD"  # a hold stays a hold


def test_ai_caution_or_disagreement_lowers_confidence_and_can_turn_a_sell_into_hold():
    from types import SimpleNamespace

    from advisor_svc.ai.base import Verdict
    from advisor_svc.ai.review import apply_ai_to_verdict

    def rec(consensus):
        return SimpleNamespace(scope="holding", name="Canara Bank", symbol="CANBK", ai_consensus=consensus, confidence=0.68, bucket="urgent",
                               action="EXIT", headline="", actionable=True,
                               short_report={"rule_action": "EXIT", "rules_confidence": 0.68, "urgent": True,
                                             "rule_suggestion": {"type": "sell", "note": "Sell the full position"}})
    r = rec("needs_review")
    apply_ai_to_verdict(r, [Verdict("gemini", "g", "disagree")])
    assert r.action == "HOLD" and r.confidence == 0.43 and r.headline == "Hold Canara Bank" and r.bucket == "fyi" and not r.actionable
    assert "Gemini disagreed" in r.short_report["do"]
    r = rec("verified")
    apply_ai_to_verdict(r, [Verdict("gemini", "g", "agree")])
    assert r.action == "EXIT" and r.confidence == 0.78 and r.short_report["conviction"] == "high"   # AI agreement → a firm call
    assert r.short_report["rules_confidence"] == 0.68 and r.short_report["ai_adjusted"]
    r = rec("caution")
    apply_ai_to_verdict(r, [Verdict("gemini", "g", "caution")])
    assert r.action == "HOLD" and r.confidence == 0.58  # the AI's doubts push a medium call below the line
    r = rec("verified")
    apply_ai_to_verdict(r, [Verdict("gemini", "g", "agree")])
    apply_ai_to_verdict(r, [Verdict("gemini", "g", "agree")])
    assert r.confidence == 0.78  # applying again (e.g. on a rerun) never compounds


def test_no_buy_or_sell_call_without_fundamentals_or_price_history():
    # a stock in a drawdown with no company data: hold and say data is missing — never exit / accumulate on half the picture
    d = decide(dq__no_fundamentals=True, dd__triggered=True, dd__driver="stock_specific", fund__score=None, tech__above_200dma=False)
    assert d.id == "data_no_fundamentals" and d.action == "REVIEW"
    assert decide(dq__no_history=True, fund__score=80, tech__score=70).id == "data_no_price_history"
    assert decide(asset_type="mutual_fund", dq__no_history=True, mf__is_regular=True).action == "REVIEW"
    # your own stop-loss still fires — it needs only the price
    assert decide(intent="trade", trade__stop_hit=True, dq__no_fundamentals=True).action == "EXIT"
    # a mutual fund has no company fundamentals by design: not gated on them
    assert decide(asset_type="mutual_fund", dq__no_fundamentals=True, mf__is_regular=True).action == "SWITCH"


def test_data_gate_verdict_reads_as_hold():
    from advisor_svc.report import verdict_for

    v = verdict_for("REVIEW", 0.5, "TATAPOWER", None)
    assert v["label"] == "Hold · data missing" and v["headline"] == "Hold TATAPOWER — data missing"
    assert v["do"] == "Do nothing for now — keep holding until the missing data loads."


def test_ai_can_stop_a_trade_but_never_start_one():
    from types import SimpleNamespace

    from advisor_svc.ai.base import Verdict
    from advisor_svc.ai.review import apply_ai_to_verdict

    def rec(rule_action, consensus, sugg=None):
        return SimpleNamespace(scope="holding", name="Canara Bank", symbol="CANBK", ai_consensus=consensus, confidence=0.8, bucket="urgent",
                               action=rule_action, headline="", actionable=True, asset_type="stock",
                               short_report={"rule_action": rule_action, "rules_confidence": 0.8, "urgent": True, "price": 100,
                                             "rule_suggestion": sugg or {"type": "sell", "quantity": 100, "value": 10_000, "note": "Sell the full position"}})
    # confident disagreement suggesting HOLD → the sell becomes a hold, with the AI's reason
    r = rec("EXIT", "needs_review")
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "disagree", 0.8, suggested_action="HOLD", plain_verdict="The fall is market-wide; the business is sound.")])
    assert r.action == "HOLD" and r.short_report["ai_overruled"] and "market-wide" in r.short_report["do"]
    # disagreement suggesting "sell part" → sell half instead of all
    r = rec("EXIT", "needs_review")
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "disagree", 0.7, suggested_action="TRIM", plain_verdict="Book part, keep the rest.")])
    assert r.action == "TRIM" and r.short_report["suggestion"]["quantity"] == 50
    # low-confidence disagreement: no overrule (confidence still drops)
    r = rec("EXIT", "needs_review")
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "disagree", 0.4, suggested_action="HOLD")])
    assert not r.short_report["ai_overruled"] and r.confidence == 0.55
    # AI wants a trade the rules didn't make → shown, not applied
    r = rec("HOLD", "caution", sugg={"type": "none"})
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "caution", 0.9, suggested_action="EXIT", plain_verdict="Sell before results.")])
    assert r.action == "HOLD" and r.short_report["ai_suggests"]["action"] == "EXIT" and r.short_report["ai_suggests"]["applied"] is False
    # a data-gate hold isn't 'verified': score unchanged, the AI's view is only recorded
    r = rec("REVIEW", "verified", sugg={"type": "none"})
    r.short_report["rules_confidence"] = 0.45
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "agree", 0.9, suggested_action="REVIEW", plain_verdict="Agree — wait for data.")])
    assert r.confidence == 0.8 and r.short_report["ai_view"]["plain"] == "Agree — wait for data." and "ai_adjusted" not in r.short_report


def test_reviewer_rotates_to_a_different_ai():
    from types import SimpleNamespace

    from advisor_svc.ai.review import rotate

    chain = [SimpleNamespace(provider=p) for p in ("groq", "gemini", "claude")]
    assert [c.provider for c in rotate(chain, {"groq"})] == ["gemini", "claude", "groq"]


def test_missing_data_asks_the_ai_for_its_own_opinion():
    from types import SimpleNamespace

    from advisor_svc.ai.base import OPINION_SYSTEM, REVIEW_SYSTEM, Verdict, system_for
    from advisor_svc.ai.review import apply_ai_to_verdict, evidence_packet

    r = SimpleNamespace(scope="holding", name="Tata Power", symbol="TATAPOWER.NS", asset_type="stock", ai_consensus="verified",
                        confidence=0.5, bucket="fyi", action="REVIEW", headline="Hold Tata Power — data missing", actionable=True,
                        short_report={"rule_action": "REVIEW", "rules_confidence": 0.5},
                        full_report={"data_quality": {"issues": [{"message": "No fundamentals available"}]}})
    packet = evidence_packet(r)
    assert packet["mode"] == "opinion" and packet["missing"] == ["No fundamentals available"] and system_for(packet) is OPINION_SYSTEM
    assert system_for({"recommendation": {}}) is REVIEW_SYSTEM
    apply_ai_to_verdict(r, [Verdict("groq", "llama", "agree", 0.6, rationale="Regulated utility; steady cash flows.",
                                    counter_case="High debt; power-tariff risk.", suggested_action="HOLD",
                                    plain_verdict="Hold for the long term.", horizon="long_term")])
    op = r.short_report["ai_opinion"]
    assert (op["label"], op["horizon"], op["why"]) == ("Hold", "long_term", "Regulated utility; steady cash flows.")
    assert r.headline == "Tata Power — AI opinion: Hold (long-term)" and "check with your CA" in r.short_report["do"]
    assert r.action == "REVIEW" and r.confidence == 0.5   # an opinion never becomes an automatic action or a score


def _big_report_rec(**kw):
    """A recommendation with a full-size report: chart series, every rule evaluated, long lists."""
    from types import SimpleNamespace

    series = {k: [{"date": f"2026-01-{i % 28 + 1:02d}", "v": 100.0 + i} for i in range(400)] for k in ("sma50", "sma200", "rsi", "macd")}
    full = {
        "evidence": {
            "chart_series": series,
            "technical": {"values": {"price": 100, "rsi14": 55.5, "sma50": 98, "sma200": 90, "high_52w_date": "2026-01-01"},
                          "signals": [{"label": f"Signal {i}", "evidence": "x" * 200} for i in range(30)], "series": series},
            "fundamental": {"available": True, "score": 70, "pillars": {"quality": {"score": 80}},
                            "metrics": [{"key": f"m{i}", "value": i, "label": "l" * 50, "threshold": "t" * 50} for i in range(40)]},
            "position": {"quantity": 10, "avg_cost": 90, "note": "n" * 300}, "tax": {"lots": [{"q": 1}] * 200, "days_to_ltcg": 40},
        },
        "decision_trace": {"matched_rule": "trim_overweight", "rules_evaluated": [{"rule": f"r{i}", "failed_condition": {"x": "y" * 80}} for i in range(60)]},
        "data_quality": {"grade": "B", "score": 80, "issues": [{"message": "m" * 400}] * 12},
        "risks_and_counter_arguments": ["r" * 1000] * 10, "history": [{"at": "2026-01-01", "action": "HOLD", "confidence": 0.6, "junk": "j" * 900}] * 20,
    }
    base = dict(scope="holding", name="Example", symbol="EXM.NS", asset_type="stock", full_report=full,
                short_report={"action": "TRIM", "rule_action": "TRIM", "reasons": ["why " * 100] * 12, "do": "Sell 3 shares"})
    return SimpleNamespace(**{**base, **kw})


def test_ai_evidence_packet_stays_small():
    """Free AI tiers reject big requests (Groq: 8 k tokens/minute) — the packet must stay compact, whatever the report."""
    import orjson

    from advisor_svc.ai.review import PACKET_MAX_CHARS, evidence_packet

    rec = _big_report_rec()
    packet = evidence_packet(rec)
    size = len(orjson.dumps(packet, default=str))
    assert size <= PACKET_MAX_CHARS, size
    assert "chart_series" not in orjson.dumps(packet).decode() and "rules_evaluated" not in packet["decision_trace"]
    assert packet["decision_trace"]["matched_rule"] == "trim_overweight" and packet["recommendation"]["do"] == "Sell 3 shares"
    slim = evidence_packet(rec, slim=True)
    assert len(orjson.dumps(slim, default=str)) < size


def test_ai_error_kinds():
    """Too-large requests are retried smaller (never paused); per-minute limits pause briefly; quota pauses a day."""
    from advisor_svc.ai.registry import PAUSE_MINUTE_SECONDS, PAUSE_SECONDS, error_kind, is_quota_error, pause_seconds
    from advisor_svc.ai.review import _retry_after

    too_big = {"status": 413, "error": "Request too large for model `openai/gpt-oss-20b` on tokens per minute (TPM): Limit 8000, Requested 9500"}
    per_min = {"status": 429, "error": "Rate limit reached for model on tokens per minute (TPM): Limit 8000, Used 7000. Please try again in 7.5s."}
    daily = {"status": 429, "error": "Rate limit reached on tokens per day (TPD): Limit 200000"}
    gemini = {"status": 429, "error": "Resource has been exhausted (e.g. check quota)."}
    assert error_kind(too_big) == "too_large" and not is_quota_error(too_big)
    assert error_kind(per_min) == "rate_minute" and pause_seconds("rate_minute") == PAUSE_MINUTE_SECONDS
    assert error_kind(daily) == "quota" and error_kind(gemini) == "quota" and pause_seconds("quota") == PAUSE_SECONDS
    assert error_kind({"status": 401, "error": "Invalid API key"}) is None
    assert _retry_after(per_min["error"]) == 8.5 and _retry_after("try again in 450ms") == 1.45 and _retry_after("busy") == 20.0


def test_failed_and_unchecked_calls_are_queued_for_ai_again():
    from types import SimpleNamespace

    from advisor_svc.engine import review_queue

    def r(i, consensus, actionable=True, prio=1.0, scope="holding", stale=False):
        return SimpleNamespace(id=f"id{i}", ai_consensus=consensus, actionable=actionable, priority=prio, scope=scope,
                               short_report={"ai_stale": {"reviewed_at": "2026-09-29"}} if stale else {})
    recs = [r(1, "verified"), r(2, "ai_failed", prio=5), r(3, "pending", actionable=False, prio=9), r(4, "rules_only", prio=2),
            r(5, "pending", scope="profile"), r(6, "verified", prio=1.5, stale=True)]
    # failed ones retried; a review of older inputs is re-checked (but stays shown meanwhile); trades before holds
    assert review_queue(recs) == ["id2", "id4", "id6", "id3"]


def test_broken_thesis_sells_half_now_and_the_rest_if_it_does_not_recover():
    first = decide(thesis__failed_count=1)
    assert (first.id, first.action, first.trim_fraction) == ("thesis_failed", "TRIM", 0.5)
    assert decide(thesis__failed_count=1, tech__score=30).action == "EXIT"  # and the trend has broken too
    assert decide(thesis__failed_count=1, watch__days=45).id == "thesis_failed_persistent"


def test_weak_core_holding_is_trimmed_then_sold_never_left_as_data_missing():
    weak = decide(fund__score=30, tech__score=30)
    assert (weak.action, weak.trim_fraction) == ("TRIM", 0.5)
    assert decide(fund__score=30, tech__score=30, watch__days=50).action == "EXIT"
    # REVIEW now only ever means "data missing"
    assert all(r.id.startswith("data_") for r in load_rulebook().rules if r.action == "REVIEW")


def test_quality_on_sale_waits_for_the_price_to_stabilise_and_skips_satellites_in_a_bear():
    dip = {"dd__triggered": True, "dd__driver": "market", "fund__score": 75}
    assert decide(**dip).id == "quality_on_sale"
    waiting = decide(**dip, tech__stabilised=False)
    assert (waiting.id, waiting.action, waiting.actionable) == ("quality_on_sale_wait", "HOLD", False)
    assert decide(**dip, tech__stabilised=None).action == "HOLD"  # unknown is not "stabilised"
    assert decide(**dip, intent="satellite", regime="bear", regime__caution=True, regime__blocks_new_risk=True).action != "ACCUMULATE"
    assert decide(fund__score=80, tech__score=70, regime__blocks_new_risk=True).action != "ADD"


def test_euphoria_protects_the_gain_with_a_trailing_stop_instead_of_selling_a_winner():
    from advisor_svc.report import do_line, suggestion

    facts = {**BASE, "regime": "euphoria", "tech.rsi": 80, "fund.valuation": 20, "pos.unrealised_pct": 60, "trade.stop_price": 1450.0,
             "trade.stop_source": "auto"}
    d = evaluate(facts)
    assert (d.rule.id, d.rule.action) == ("euphoria_protect_gains", "HOLD")
    s = suggestion("HOLD", {"asset_type": "stock", "price": 1600, "quantity": 10, "market_value": 16000}, facts, {}, 1e6, d.rule)
    assert s["type"] == "trail_stop" and s["stop"] == 1450.0
    line = do_line("HOLD", "high", s, "stock", 1600)
    assert "stop-loss" in line and ("1,450" in line or "1450" in line)


def test_trade_stop_is_automatic_and_only_ratchets_up():
    from advisor_svc.facts import trade_stop

    row = {"avg_cost": 100.0}
    assert trade_stop(row, {"atr14": 5.0, "chandelier_stop": 90.0}, None) == (90.0, "auto")  # initial stop 100 − 2×5
    assert trade_stop(row, {"atr14": 5.0, "chandelier_stop": 118.0}, None) == (118.0, "auto")  # price ran up: the stop follows
    assert trade_stop(row, {"atr14": 5.0, "chandelier_stop": 118.0}, {"stop_price": 95}) == (95.0, "yours")
    assert trade_stop({}, {}, None) == (None, None)
    # a setup has failed when it loses its 20-day average, not only its 200-day one
    assert decide(intent="trade", dd__triggered=True, tech__above_20dma=False, tech__above_200dma=True).id == "trade_setup_failed"


def test_buy_sizing_respects_volatility_and_halves_in_a_correction():
    from advisor_svc.report import suggestion

    row = {"asset_type": "stock", "price": 100.0, "quantity": 10, "market_value": 1000.0}
    ev = {"sizing": {"weight_pct": 1.0, "cap_pct": 10, "add_value_at_1pct_risk": 5000.0}}
    calm = suggestion("ADD", row, {**BASE}, ev, 1e6)
    assert calm["value"] == 5000.0 and "1% of the portfolio" in calm["note"]  # 2% of 10 lakh = 20,000, capped by risk
    rough = suggestion("ADD", row, {**BASE, "regime.caution": True}, ev, 1e6)
    assert rough["value"] == 2500.0 and "correction" in rough["note"]


def test_regular_to_direct_switch_waits_for_long_term_tax():
    from advisor_svc.report import suggestion

    row = {"asset_type": "mutual_fund", "price": 50.0, "quantity": 100, "market_value": 5000.0}
    soon = suggestion("SWITCH", row, {**BASE, "mf.is_regular": True, "tax.days_to_ltcg": 120}, {}, 1e6)
    assert soon["switch_after"] and soon["note"].startswith("Start new SIPs in the Direct plan now")
    now = suggestion("SWITCH", row, {**BASE, "mf.is_regular": True, "tax.days_to_ltcg": 0}, {}, 1e6)
    assert now["switch_after"] is None and now["note"] == "Switch to the Direct plan of the same scheme"
