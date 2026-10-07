"""Signal Collector (E17) + Drawdown Sentinel (E20) + Position Sizing (E39) + tax facts (E22).

Produces a flat ``facts`` dict for the rulebook plus a structured ``evidence`` dict for the
report. Every number in the evidence carries its source and as-of timestamp."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import taxrules

DEFAULT_DD = {"trade": 8.0, "satellite": 15.0, "core": 25.0, "retirement": 30.0, "goal": 20.0}
SINGLE_CAP = {  # % of the profile's portfolio
    "conservative": {"stock": 8.0, "mutual_fund": 30.0, "etf": 60.0},
    "moderate": {"stock": 10.0, "mutual_fund": 35.0, "etf": 60.0},
    "aggressive": {"stock": 15.0, "mutual_fund": 40.0, "etf": 70.0},
}
DQ_MULT = {"A": 1.0, "B": 0.9, "C": 0.72, "D": 0.45}

_CLASS = {"stock": "equity", "etf": "equity", "reit": "real_estate", "epf": "debt", "vpf": "debt", "ppf": "debt", "fixed_deposit": "debt",
          "bond": "debt", "gold": "gold", "crypto": "alternative", "cash": "cash"}


def ASSET_CLASS_OF(row: dict[str, Any]) -> str:  # noqa: N802 — mirrors portfolio-svc's asset_class()
    meta = row.get("meta") or {}
    if row["asset_type"] == "mutual_fund":
        cat = str(meta.get("mf_category") or "equity")
        return "debt" if cat.startswith("debt") else "hybrid" if cat.startswith("hybrid") else "equity"
    if row["asset_type"] == "nps":
        return {"E": "equity", "C": "debt", "G": "debt", "A": "alternative"}.get(meta.get("scheme", "E"), "equity")
    if row["asset_type"] == "etf" and "gold" in (row.get("name") or "").lower():
        return "gold"
    return _CLASS.get(row["asset_type"], "other")


def drawdown(row: dict[str, Any], analysis: dict[str, Any], threshold: float) -> dict[str, Any]:
    tech = (analysis.get("technical") or {}).get("values") or {}
    from_cost = row.get("unrealised_pct")
    from_high = tech.get("pct_from_52w_high")
    worst = min(v for v in (from_cost, from_high, 0.0) if v is not None)
    triggered = worst <= -threshold
    basis = None
    if triggered:
        basis = "cost" if from_cost is not None and from_cost <= -threshold and (from_high is None or from_cost <= from_high) else "52w_high"
    return {
        "from_cost_pct": from_cost, "from_high_pct": from_high, "threshold_pct": threshold, "triggered": triggered, "basis": basis,
        "since_date": (row.get("oldest_open_lot_date") if basis == "cost" else tech.get("high_52w_date")) if triggered else None,
    }


def sizing(row: dict[str, Any], analysis: dict[str, Any], profile_value: float, risk_profile: str, pref: dict[str, Any] | None) -> dict[str, Any]:
    weight = row["market_value"] / profile_value * 100 if profile_value else 0.0
    caps = SINGLE_CAP.get(risk_profile, SINGLE_CAP["moderate"])
    cap = caps.get(row["asset_type"])
    tech = (analysis.get("technical") or {}).get("values") or {}
    atr = tech.get("atr14")
    price = row.get("price")
    out: dict[str, Any] = {"weight_pct": round(weight, 2), "cap_pct": cap, "over_cap": bool(cap and weight > cap * 1.1)}
    if price and atr:
        stop = (pref or {}).get("stop_price") or round(price - 2 * atr, 2)
        out["atr_stop"] = round(price - 2 * atr, 2)
        out["stop_price"] = stop
        out["risk_per_unit"] = round(price - stop, 2)
        out["max_units_at_1pct_risk"] = int(profile_value * 0.01 / (price - stop)) if price > stop else None
        # volatility sizing for new money: a 2 × ATR fall on what you add should cost at most 1% of the portfolio
        out["add_value_at_1pct_risk"] = round(profile_value * 0.01 / (2 * atr) * price, 2)
    return out


def trade_stop(row: dict[str, Any], tv: dict[str, Any], pref: dict[str, Any] | None) -> tuple[float | None, str | None]:
    """A trade's stop-loss: yours if you set one; otherwise automatic — the higher of the initial stop (cost − 2 × ATR)
    and the trailing Chandelier stop (22-day high − 3 × ATR), so it follows the price up and never moves down.
    (It used to be derived from today's price, so it moved with the price and could never be hit.)"""
    if (pref or {}).get("stop_price"):
        return float(pref["stop_price"]), "yours"
    atr, cost = tv.get("atr14"), row.get("avg_cost")
    candidates = [c for c in (round(cost - 2 * atr, 2) if atr and cost else None, tv.get("chandelier_stop")) if c]
    return (max(candidates), "auto") if candidates else (None, None)


def thesis_results(pref: dict[str, Any] | None, facts: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    checks = (pref or {}).get("thesis_checks") or []
    metric_values = {m["key"]: m["value"] for m in ((analysis.get("fundamental") or {}).get("metrics") or [])}
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b, "==": lambda a, b: a == b}
    out = []
    for c in checks:
        metric = c.get("metric")
        actual = facts.get(metric, metric_values.get(metric))
        ok = None if actual is None or c.get("op") not in ops else ops[c["op"]](actual, c.get("value"))
        out.append({"metric": metric, "op": c.get("op"), "expected": c.get("value"), "actual": actual, "holds": ok})
    return out


def build(
    row: dict[str, Any], analysis: dict[str, Any], attribution: dict[str, Any] | None, profile: dict[str, Any], profile_value: float,
    pref: dict[str, Any] | None, intent: str, regime: dict[str, Any], today: date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    today = today or date.today()
    tech = analysis.get("technical") or {}
    tv = tech.get("values") or {}
    fund = analysis.get("fundamental") or {}
    mf = analysis.get("fund") or {}
    dq = analysis.get("data_quality") or {"grade": "B", "score": 75, "issues": []}
    meta = {**(row.get("meta") or {}), "name": row.get("name")}

    thresholds = {**DEFAULT_DD, **{k: float(v) for k, v in (profile.get("drawdown_thresholds") or {}).items()}}
    thr = float((pref or {}).get("drawdown_threshold_pct") or thresholds.get(intent, 20.0))
    dd = drawdown(row, analysis, thr)
    if dd["triggered"] and attribution and attribution.get("available"):
        dd["attribution"] = attribution
        dd["driver"] = attribution["dominant_driver"]
    size = sizing(row, analysis, profile_value, profile.get("risk_profile") or "moderate", pref)
    tax = taxrules.timing(row, meta, profile.get("tax_slab_pct"), today)

    price = row.get("price")
    target = (pref or {}).get("target_price")
    stop, stop_source = trade_stop(row, tv, pref)
    size["trade_stop"], size["trade_stop_source"] = stop, stop_source
    caution = regime.get("regime") in ("correction", "bear")
    pillars = (fund.get("pillars") or {})
    facts: dict[str, Any] = {
        "asset_type": row["asset_type"], "intent": intent, "regime": regime.get("regime", "unknown"), "dq.grade": dq.get("grade"),
        # hard data gates: no buy/sell call on a stock we can't value, or on anything whose trend we can't see
        "dq.no_fundamentals": row["asset_type"] == "stock" and (fund.get("score") is None
                                                               or any(i.get("code") == "no_fundamentals" for i in dq.get("issues") or [])),
        "dq.no_history": row["asset_type"] not in ("epf", "vpf", "ppf", "nps", "fixed_deposit", "bond", "cash")
                         and any(i.get("code") == "short_history" for i in dq.get("issues") or []),
        "tech.score": tech.get("score"), "tech.trend": tech.get("trend"), "tech.rsi": tv.get("rsi14"),
        "tech.above_200dma": (price > tv["sma200"]) if price and tv.get("sma200") else None,
        "tech.above_50dma": (price > tv["sma50"]) if price and tv.get("sma50") else None,
        "tech.above_20dma": (price > tv["sma20"]) if price and tv.get("sma20") else None,
        # "has it stopped falling?": no fresh 20-day low for 5 sessions and back above its 20-day average
        "tech.stabilised": (bool(price > tv["sma20"] and tv["days_since_20d_low"] >= 5)
                            if price and tv.get("sma20") and tv.get("days_since_20d_low") is not None else None),
        "tech.pct_from_high": tv.get("pct_from_52w_high"), "tech.rs_3m": tv.get("relative_strength_3m_pct"),
        "fund.score": fund.get("score"), "fund.valuation": (pillars.get("valuation") or {}).get("score"),
        "fund.red_flag_count": len(fund.get("red_flags") or []),
        "mf.score": mf.get("score"), "mf.consistency": mf.get("consistency_pct"), "mf.alpha": mf.get("alpha_pct"),
        "mf.is_regular": _is_regular(row, meta, mf),
        "mf.excess_cost": (mf.get("cost") or {}).get("excess_cost_pct"),
        "mf.is_index": str(meta.get("mf_category") or "").endswith("index") or "index" in str(row.get("name") or "").lower(),
        "dd.triggered": dd["triggered"], "dd.driver": dd.get("driver"), "dd.threshold": thr,
        "dd.from_cost_pct": dd["from_cost_pct"], "dd.from_high_pct": dd["from_high_pct"],
        "pos.weight_pct": size["weight_pct"], "pos.over_cap": size["over_cap"], "pos.unrealised_pct": row.get("unrealised_pct"),
        "pos.holding_days": None if row.get("dates_estimated") else row.get("holding_days"),
        "trade.stop_hit": bool(stop and price and price <= stop), "trade.target_hit": bool(target and price and price >= target),
        "trade.stop_price": stop, "trade.stop_source": stop_source,
        # correction / bear: keep SIPs and core buying (smaller), but no new money into satellite ideas or trades
        "regime.caution": caution, "regime.blocks_new_risk": caution and intent in ("satellite", "trade"),
        "watch.days": 0,  # the engine sets how long this holding has been flagged (thesis broken / weak core)
        "tax.days_to_ltcg": tax.get("days_to_ltcg"), "tax.saving_if_wait": tax.get("saving_if_wait"),
    }
    thesis = thesis_results(pref, facts, analysis)
    facts["thesis.failed_count"] = sum(1 for t in thesis if t["holds"] is False)

    evidence = {
        "position": {**{k: row.get(k) for k in ("quantity", "avg_cost", "price", "invested", "market_value", "unrealised_pnl", "unrealised_pct",
                                                   "xirr_pct", "holding_days", "first_buy_date", "realised_pnl", "dividends")},
                     # a holdings statement has no purchase dates: say so instead of "held 0 days since today"
                     **({"holding_days": None, "first_buy_date": None, "dates_estimated": True,
                         "note": "Purchase dates unknown (imported from a holdings statement); holding period and tax lots are estimates."}
                        if row.get("dates_estimated") else {})},
        "technical": tech, "fundamental": fund, "fund": mf, "risk": analysis.get("risk"), "market_beta": analysis.get("market_beta"),
        "drawdown": dd, "sizing": size, "tax": tax, "thesis": thesis, "regime": regime, "data_quality": dq,
        "quote": analysis.get("quote"), "benchmark_symbol": analysis.get("benchmark_symbol"),
    }
    return facts, evidence


def attribution_since(dd: dict[str, Any]) -> str | None:
    s = dd.get("since_date")
    if not s:
        return None
    # attribution over >1y is dominated by drift; cap the window at 1 year
    return max(s, (date.today() - timedelta(days=365)).isoformat())


def _is_regular(row: dict[str, Any], meta: dict[str, Any], mf: dict[str, Any] | None) -> bool | None:
    """Regular-plan only when the scheme name says so (or explicitly 'Regular' and not 'Direct')."""
    if row["asset_type"] != "mutual_fund":
        return None
    name = (row.get("name") or "").lower()
    if "direct" in name:
        return False
    flagged = (mf.get("cost") or {}).get("is_regular_plan") if mf else meta.get("plan") == "regular"
    return bool(flagged) and "regular" in name
