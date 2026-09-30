"""Portfolio Health & X-Ray Engine (E26) + portfolio-level findings (allocation drift,
sector concentration, fund clutter). Produces REBALANCE/REVIEW recommendations per profile."""
from __future__ import annotations

from datetime import date
from typing import Any

SECTOR_CAP = {"conservative": 25.0, "moderate": 30.0, "aggressive": 35.0}
LOCKED = {"epf", "vpf", "ppf", "nps"}  # can't be sold/switched freely — rebalance with new money instead


def default_target(profile: dict[str, Any]) -> dict[str, float]:
    age = profile.get("age") or 35
    rp = profile.get("risk_profile") or "moderate"
    equity = {"conservative": 100 - age - 10, "moderate": 110 - age, "aggressive": 120 - age}[rp]
    equity = float(max(20, min(85, equity)))
    gold = 10.0
    return {"equity": equity, "debt": round(100 - equity - gold, 1), "gold": gold}


NOT_A_SECTOR = {"diversified", "others", "other", "etf", "etfs", "unclassified", "index", "miscellaneous", ""}


def findings(profile: dict[str, Any], summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    value = summary.get("market_value") or 0
    if value <= 0:
        return out
    alloc = {k: v["pct"] for k, v in (summary.get("allocation") or {}).items()}
    target = profile.get("target_allocation") or default_target(profile)
    source = "your target" if profile.get("target_allocation") else f"a {profile.get('risk_profile', 'moderate')} profile at age {profile.get('age') or 'unknown'}"
    drifts = {k: round(alloc.get(k, 0.0) - float(t), 1) for k, t in target.items()}
    big = {k: d for k, d in drifts.items() if abs(d) >= 10}
    if big:
        over = max(big, key=lambda k: big[k])
        under = min(drifts, key=lambda k: drifts[k])
        move = min(abs(big.get(over, 0)), abs(drifts[under])) / 100 * value
        # How much of the over-weight class is locked (EPF/PPF/NPS) and therefore not sellable?
        from .facts import ASSET_CLASS_OF  # local import to avoid a cycle
        locked = sum(r["market_value"] for r in rows if r["asset_type"] in LOCKED and ASSET_CLASS_OF(r) == over)
        over_value = alloc.get(over, 0) / 100 * value
        movable = max(0.0, over_value - locked)
        mix = "Current mix: " + ", ".join(f"{k} {v:.0f}%" for k, v in sorted(alloc.items(), key=lambda kv: -kv[1]))
        tgt = f"Target from {source}: " + ", ".join(f"{k} {v:.0f}%" for k, v in target.items())
        if locked and movable < move:
            out.append({
                "rule_id": "allocation_drift_locked", "action": "REBALANCE", "severity": 2, "confidence": 0.8, "impact": move,
                "headline": f"Direct new investments to {under} — {over} is {drifts[over]:+.0f} pp over target, mostly in locked retirement accounts",
                "reasons": [mix, tgt,
                            f"₹{locked:,.0f} of your {over} is EPF/PPF/NPS, which can't (and shouldn't) be withdrawn to rebalance. "
                            f"Instead, route new SIPs/lump sums to {under} until the gap (~₹{move:,.0f}) closes"
                            + (f"; only ₹{movable:,.0f} of {over} is freely movable." if movable else ".")],
                "evidence": {"allocation_pct": alloc, "target_pct": target, "drift_pp": drifts, "locked_value": locked, "movable_value": movable},
                "what_would_change": "Resolved when every asset class is within ±10 pp of target (new money counts).",
                "do": f"Don't sell anything. Put all new SIPs / lump sums into {under} until about ₹{move:,.0f} has gone in.",
            })
        else:
            out.append({
                "rule_id": "allocation_drift", "action": "REBALANCE", "severity": 3, "confidence": 0.8, "impact": move,
                "headline": f"Rebalance: {over} is {drifts[over]:+.0f} pp vs target, {under} {drifts[under]:+.0f} pp",
                "reasons": [mix, tgt, f"Moving ~₹{move:,.0f} from {over} to {under} restores the plan; prefer redirecting new SIPs before selling (no tax)."],
                "evidence": {"allocation_pct": alloc, "target_pct": target, "drift_pp": drifts},
                "what_would_change": "Resolved when every asset class is within ±10 pp of target.",
                "do": f"Move about ₹{move:,.0f} from {over} to {under} — first by sending new SIPs to {under}; sell {over} only for what's left.",
            })
    cap = SECTOR_CAP.get(profile.get("risk_profile") or "moderate", 30.0)
    for sector, v in (summary.get("by_sector") or {}).items():
        if sector.strip().lower() in NOT_A_SECTOR:
            continue  # index funds / ETFs / unknowns aren't a single-sector bet
        if v["pct"] > cap:
            out.append({
                "rule_id": f"sector_concentration:{sector}", "action": "REBALANCE", "severity": 2, "confidence": 0.75,
                "impact": (v["pct"] - cap) / 100 * value,
                "headline": f"{sector} is {v['pct']:.0f}% of the portfolio (cap {cap:.0f}%)",
                "reasons": [f"Direct stock exposure to {sector}: ₹{v['value']:,.0f} ({v['pct']:.1f}%)", f"Your sector cap: {cap:.0f}%",
                            "A single sector shock (regulation, cycle) would hit a large part of your wealth."],
                "evidence": {"by_sector": summary.get("by_sector")}, "what_would_change": f"Resolved when {sector} ≤ {cap:.0f}%.",
                "do": f"Stop buying more {sector} stocks; put new money into other sectors until {sector} is under {cap:.0f}% "
                      f"(about ₹{(v['pct'] - cap) / 100 * value:,.0f} too much today).",
            })
    equity_funds = [r for r in rows if r["asset_type"] == "mutual_fund" and str((r.get("meta") or {}).get("mf_category", "equity")).startswith("equity") and r["quantity"] > 0]
    if len(equity_funds) > 7:
        out.append({
            "rule_id": "fund_clutter", "action": "REVIEW", "severity": 2, "confidence": 0.7, "impact": 0.0,
            "headline": f"{len(equity_funds)} equity funds — likely heavy overlap",
            "reasons": ["Beyond 5–7 equity funds, extra funds mostly duplicate the same stocks and dilute performance.",
                        "Consolidate into 1 index + 2–4 active funds across categories."],
            "evidence": {"funds": [f["name"] for f in equity_funds]}, "what_would_change": "Resolved at ≤ 7 equity funds.",
            "do": "Stop new SIPs in the overlapping funds; keep 1 index fund + 2–4 active funds and let the rest run down (mind exit loads and tax).",
        })
    return out


def health_score(summary: dict[str, Any], rows: list[dict[str, Any]], verdicts: dict[str, str], profile: dict[str, Any]) -> dict[str, Any]:
    value = summary.get("market_value") or 0
    if not value:
        return {"score": None}
    weights = [r["market_value"] / value for r in rows if r["market_value"] > 0]
    hhi = sum(w * w for w in weights)
    diversification = max(0.0, min(100.0, (1 - hhi) * 110))
    alloc = {k: v["pct"] for k, v in (summary.get("allocation") or {}).items()}
    target = profile.get("target_allocation") or default_target(profile)
    drift = sum(abs(alloc.get(k, 0) - float(t)) for k, t in target.items()) / 2
    allocation_fit = max(0.0, 100 - drift * 2)
    flagged = sum(r["market_value"] for r in rows if verdicts.get(r["instrument_id"]) in ("EXIT", "REVIEW"))
    quality = max(0.0, 100 - flagged / value * 200)
    regular = sum(r["market_value"] for r in rows if (r.get("meta") or {}).get("plan") == "regular")
    cost = max(0.0, 100 - regular / value * 150)
    dd = sum(r["market_value"] for r in rows if (r.get("unrealised_pct") or 0) <= -20)
    drawdown = max(0.0, 100 - dd / value * 150)
    parts = {"diversification": diversification, "allocation_fit": allocation_fit, "holding_quality": quality, "cost_efficiency": cost, "drawdown_exposure": drawdown}
    weights_ = {"diversification": 0.2, "allocation_fit": 0.25, "holding_quality": 0.3, "cost_efficiency": 0.1, "drawdown_exposure": 0.15}
    score = round(sum(parts[k] * w for k, w in weights_.items()), 1)
    return {
        "score": score, "grade": "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D",
        "components": {k: round(v, 1) for k, v in parts.items()}, "weights": weights_,
        "xray": {"hhi": round(hhi, 4), "effective_holdings": round(1 / hhi, 1) if hhi else None, "allocation_pct": alloc,
                 "target_pct": target, "top_holdings": sorted(({"name": r["name"], "pct": round(r["market_value"] / value * 100, 2)} for r in rows), key=lambda x: -x["pct"])[:5]},
        "as_of": date.today().isoformat(),
    }
