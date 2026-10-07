"""Portfolio Health & X-Ray Engine (E26) + portfolio-level findings (allocation drift,
sector concentration, fund clutter). Produces REBALANCE/REVIEW recommendations per profile."""
from __future__ import annotations

import re
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


def findings(profile: dict[str, Any], summary: dict[str, Any], rows: list[dict[str, Any]],
             look: dict[str, Any] | None = None, funds: dict[str, dict[str, Any]] | None = None,
             plan: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """``look``: MF look-through for this profile (fm_common.lookthrough: combine() + "overlap") — adds hidden
    concentration through funds and redundant (overlapping) funds. ``funds``: {instrument_id: {id, name, value, price,
    lots, score, expense_ratio, regular, category}} — lets an overlap say which fund to keep and how to move out."""
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
    if len(equity_funds) > 7 and not funds:  # with per-fund facts the line-up card says it — fund by fund
        out.append({
            "rule_id": "fund_clutter", "action": "REBALANCE", "severity": 2, "confidence": 0.7, "impact": 0.0,
            "headline": f"{len(equity_funds)} equity funds — likely heavy overlap",
            "reasons": ["Beyond 5–7 equity funds, extra funds mostly duplicate the same stocks and dilute performance.",
                        "Consolidate into 1 index + 2–4 active funds across categories."],
            "evidence": {"funds": [f["name"] for f in equity_funds]}, "what_would_change": "Resolved at ≤ 7 equity funds.",
            "do": "Stop new SIPs in the overlapping funds; keep 1 index fund + 2–4 active funds and let the rest run down (mind exit loads and tax).",
        })
    out += lookthrough_findings(profile, value, look, funds, plan=plan)
    return out


STCG_RATE, LTCG_RATE, EXIT_LOAD = 0.20, 0.125, 0.01  # equity funds: < 1 year 20 %, ≥ 1 year 12.5 % · typical 1 % load within a year


def _is_elss(f: dict[str, Any]) -> bool:
    return "elss" in str(f.get("category") or "").lower() or bool(re.search(r"(?i)\belss\b|tax\s*saver", str(f.get("name") or "")))


def exit_cost(f: dict[str, Any], today: date) -> dict[str, Any]:
    """What selling all of a fund today would cost, from its lots: tax on gains (short / long term), a likely exit load
    on units under a year old, and units still in an ELSS 3-year lock-in (can't be sold yet)."""
    price, elss = float(f.get("price") or 0), _is_elss(f)
    out = {"value": 0.0, "locked_value": 0.0, "unlock_from": None, "short_value": 0.0, "short_gain": 0.0, "long_gain": 0.0,
           "loss": 0.0, "tax": 0.0, "exit_load": 0.0}
    for lot in f.get("lots") or []:
        bought = date.fromisoformat(str(lot["buy_date"])[:10])
        v = float(lot["qty"]) * price
        gain = float(lot["qty"]) * (price - float(lot.get("cost") or 0))
        age = (today - bought).days
        out["value"] += v
        if elss and age < 3 * 365:
            out["locked_value"] += v
            unlock = (bought.replace(year=bought.year + 3)).isoformat()
            out["unlock_from"] = min(out["unlock_from"] or unlock, unlock)
            continue
        out["loss"] += max(-gain, 0.0)
        if age < 365:
            out["short_value"] += v
            out["short_gain"] += max(gain, 0.0)
            out["exit_load"] += v * EXIT_LOAD
        else:
            out["long_gain"] += max(gain, 0.0)
    out["tax"] = out["short_gain"] * STCG_RATE + out["long_gain"] * LTCG_RATE
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in out.items()}


def overlap_advice(pair: dict[str, Any], fa: dict[str, Any], fb: dict[str, Any], today: date) -> dict[str, Any]:
    """Which of two overlapping funds to keep, and how to move out of the other at the least cost.

    Keep: the clearly better fund score (rolling-return consistency, alpha, risk-adjusted return, cost — 5+ points
    apart); else the direct plan over a regular one; else the clearly cheaper one (0.2 %+ lower expense ratio); else the
    one you hold more of (less to move)."""
    from .report import inr

    def why(k: dict[str, Any], o: dict[str, Any]) -> str | None:
        if k.get("score") is not None and o.get("score") is not None and k["score"] - o["score"] >= 5:
            return f"fund score {k['score']:.0f}/100 vs {o['score']:.0f}/100 (consistency vs its benchmark, alpha, risk-adjusted return, cost)"
        if not k.get("regular") and o.get("regular"):
            return "it's a direct plan and the other is a regular plan (≈0.5–1 %/yr more in commission)"
        if k.get("expense_ratio") is not None and o.get("expense_ratio") is not None and o["expense_ratio"] - k["expense_ratio"] >= 0.2:
            return f"lower cost: expense ratio {k['expense_ratio']:.2f}% vs {o['expense_ratio']:.2f}%"
        return None

    keep, ex, reason = None, None, None
    for k, o in ((fa, fb), (fb, fa)):
        if (r := why(k, o)) is not None:
            keep, ex, reason = k, o, r
            break
    if keep is None:
        keep, ex = (fa, fb) if fa["value"] >= fb["value"] else (fb, fa)
        reason = f"similar quality — you already hold more of it ({inr(keep['value'])} vs {inr(ex['value'])}), so less to move"
    c = exit_cost(ex, today)
    movable = c["value"] - c["locked_value"]
    long_value = movable - c["short_value"]
    costs = []
    if c["tax"] or c["exit_load"]:
        costs.append(f"Selling all of {ex['name']} today: est. tax {inr(c['tax'])}"
                     + (f" + exit load ≈{inr(c['exit_load'])}" if c["exit_load"] else "")
                     + " (equity funds: 20 % short-term under a year, 12.5 % long-term; first ₹1.25 lakh of long-term gains a year is tax-free)")
    if c["locked_value"]:
        costs.append(f"{inr(c['locked_value'])} of {ex['name']} is in its ELSS 3-year lock-in (first units free on {c['unlock_from']})")
    cheap = (c["tax"] + c["exit_load"]) <= 0.01 * max(movable, 1)
    if movable <= 0:
        do = (f"Stop new SIPs in {ex['name']} and start them in {keep['name']}. Its units are locked (ELSS) — "
              f"move them into {keep['name']} as each unlocks, from {c['unlock_from']}.")
    elif cheap:
        do = (f"Switch: sell {ex['name']} ({inr(movable)}) and invest it in {keep['name']} — est. cost {inr(c['tax'] + c['exit_load'])}. "
              f"Move its SIP to {keep['name']} too.")
    else:
        do = (f"Move your SIP from {ex['name']} to {keep['name']} now. "
              + (f"Units held over a year ({inr(long_value)}) can move now (long-term tax only). " if long_value > 0 else "")
              + (f"Move the rest ({inr(c['short_value'])}) as each instalment turns 1 year old — avoids 20 % short-term tax and the exit load."
                 if c["short_value"] > 0 else ""))
        if c["locked_value"]:
            do += f" ELSS units move as they unlock (from {c['unlock_from']})."
    return {"keep": keep["name"], "exit": ex["name"], "keep_id": keep.get("id"), "exit_id": ex.get("id"), "why_keep": f"Keep {keep['name']}: {reason}.",
            "costs": costs, "do": do.strip(), "move_value": round(movable, 2), "exit_cost": c}


NEUTRAL_SCORE = 60.0  # a fund whose score hasn't loaded yet is "unknown", never "worst" — no decision against it on missing data


def _keeper_rank(f: dict[str, Any]) -> tuple:
    """Best fund to keep first:
    1. not one the advisor already says to sell or switch;
    2. no lock-in (new money stays free);
    3. the fund your SIP already goes into — unless it lags. Moving a running SIP from a good fund to another good
       fund is churn, not advice;
    4. fund score (not loaded yet → neutral, never "worst"), direct plan, lower cost, bigger holding."""
    score = f["score"] if f.get("score") is not None else NEUTRAL_SCORE
    return (f.get("verdict") not in ("EXIT", "TRIM", "SWITCH"), not _is_elss(f), bool(f.get("sip_active")) and not lags(f), score,
            not f.get("regular"), -(f["expense_ratio"]) if f.get("expense_ratio") is not None else -9.0, f.get("value") or 0.0)


def move_out(ex: dict[str, Any], keep: dict[str, Any], today: date) -> dict[str, Any]:
    """How to move from ``ex`` into ``keep`` at the least cost (tax, exit load, ELSS lock-in) — from ex's lots."""
    from .report import inr

    c = exit_cost(ex, today)
    movable = c["value"] - c["locked_value"]
    long_value = movable - c["short_value"]
    costs = []
    if c["tax"] or c["exit_load"]:
        costs.append(f"Selling all of {ex['name']} today: est. tax {inr(c['tax'])}"
                     + (f" + exit load ≈{inr(c['exit_load'])}" if c["exit_load"] else "")
                     + " (equity funds: 20 % short-term under a year, 12.5 % long-term; first ₹1.25 lakh of long-term gains a year is tax-free)")
    if c["locked_value"]:
        costs.append(f"{inr(c['locked_value'])} of {ex['name']} is in its ELSS 3-year lock-in (first units free on {c['unlock_from']})")
    if c["loss"] >= 100:
        costs.insert(0, f"Selling also books a {inr(c['loss'])} loss — it cuts tax on gains you book this year, or carries forward 8 years "
                        f"(this is the same sale the Tax page lists under harvesting)")
    if movable <= 0:
        do = (f"Stop new SIPs in {ex['name']} and put them into {keep['name']}. Its units are locked (ELSS) — "
              f"move them into {keep['name']} as each unlocks, from {c['unlock_from']}.")
    elif (c["tax"] + c["exit_load"]) <= 0.01 * movable:
        do = (f"Sell {ex['name']} ({inr(movable)}) and invest it in {keep['name']} — est. cost {inr(c['tax'] + c['exit_load'])}. "
              f"Move its SIP to {keep['name']} too.")
    else:
        do = (f"Stop the SIP in {ex['name']} and start it in {keep['name']} now. "
              + (f"Sell the units held over a year ({inr(long_value)}) and invest in {keep['name']} (long-term tax only). " if long_value > 0 else "")
              + (f"Sell the rest ({inr(c['short_value'])}) as each instalment turns 1 year old — avoids 20 % short-term tax and the exit load."
                 if c["short_value"] > 0 else ""))
        if c["locked_value"]:
            do += f" ELSS units: move as they unlock (from {c['unlock_from']})."
    return {"do": do.strip(), "costs": costs, "move_value": round(movable, 2), "exit_cost": c}


def consolidation_plan(funds: dict[str, dict[str, Any]], pairs: list[dict[str, Any]], today: date,
                       min_pct: float = 50.0) -> list[dict[str, Any]]:
    """Groups of overlapping funds, each with one fund to keep and the others to move into it.

    Greedy, so every fund gets exactly one role: take the best remaining fund (``_keeper_rank``); every remaining fund
    that overlaps it by ``min_pct`` %+ moves into it; repeat. (Pair by pair would contradict itself: A over B, C over A …)"""
    ov = {frozenset((p.get("a_id"), p.get("b_id"))): p for p in pairs if p.get("a_id") and p.get("b_id") and p["overlap_pct"] >= min_pct}
    ids = sorted({i for k in ov for i in k if i in funds}, key=lambda i: _keeper_rank(funds[i]), reverse=True)
    groups = []
    while ids:
        keep_id, rest = ids[0], ids[1:]
        members = [i for i in rest if frozenset((keep_id, i)) in ov]
        ids = [i for i in rest if i not in members]
        if not members:
            continue
        keep = funds[keep_id]
        moves = []
        for i in sorted(members, key=lambda i: -ov[frozenset((keep_id, i))]["overlap_pct"]):
            ex, pair = funds[i], ov[frozenset((keep_id, i))]
            why_k = next((r for r in (_why_keep(keep, ex),) if r), None)
            moves.append({"id": i, "name": ex["name"], "value": ex.get("value"), "overlap_pct": pair["overlap_pct"],
                          "common": [c["name"] for c in pair.get("common", [])[:5]], "common_count": pair.get("common_count"),
                          "why_keep": why_k, **move_out(ex, keep, today)})
        groups.append({"keep_id": keep_id, "keep": keep["name"], "keep_code": keep.get("code"), "keep_value": keep.get("value"),
                       "keep_score": keep.get("score"), "keep_verdict": keep.get("verdict"),
                       "keep_reason": _keep_reason(keep, [funds[m["id"]] for m in moves]), "moves": moves})
    return groups


def _why_keep(k: dict[str, Any], o: dict[str, Any]) -> str | None:
    if _is_elss(o) and not _is_elss(k):
        return "no lock-in on new money (the other is an ELSS fund — 3-year lock-in)"
    if k.get("score") is not None and o.get("score") is not None and k["score"] - o["score"] >= 5:
        return f"better fund score {k['score']:.0f}/100 vs {o['score']:.0f}/100"
    if not k.get("regular") and o.get("regular"):
        return "direct plan (the other is a regular plan — higher commission)"
    if k.get("expense_ratio") is not None and o.get("expense_ratio") is not None and o["expense_ratio"] - k["expense_ratio"] >= 0.2:
        return f"lower cost: {k['expense_ratio']:.2f}% vs {o['expense_ratio']:.2f}%"
    return None


def _keep_reason(keep: dict[str, Any], others: list[dict[str, Any]]) -> str:
    from .report import inr

    bits = []
    if keep.get("sip_active") and not lags(keep):
        bits.append("your SIP already goes here and it doesn't lag — no reason to move it")
    if keep.get("score") is not None:
        scored = [o["score"] for o in others if o.get("score") is not None]
        bits.append(f"fund score {keep['score']:.0f}/100" + (f" (others {min(scored):.0f}–{max(scored):.0f})" if scored else ""))
    else:
        bits.append("fund score not loaded yet (judged on the rest)")
    if not _is_elss(keep):
        bits.append("no lock-in")
    if not keep.get("regular"):
        bits.append("direct plan")
    if keep.get("expense_ratio") is not None:
        bits.append(f"expense ratio {keep['expense_ratio']:.2f}%")
    if (keep.get("value") or 0) >= max((o.get("value") or 0 for o in others), default=0):
        bits.append(f"your biggest holding of these ({inr(keep.get('value'))})")
    return ", ".join(bits)


LINEUP_MAX = 6  # more funds than this: build a line-up (one fund per role), not pair-by-pair overlap fixes
ROLES = {"large": "Large cap / index (core)", "flexi": "Flexi / multi / large & mid cap", "mid": "Mid cap", "small": "Small cap",
         "debt": "Debt / hybrid", "gold": "Gold / silver", "intl": "International"}
_INTL = re.compile(r"(?i)nasdaq|s&p\s*500|\bus\b|u\.s\.|global|international|overseas|world|fang|hang\s*seng|japan|china|taiwan|emerging")
_DEBT = re.compile(r"(?i)liquid|gilt|bond|debt|arbitrage|balanced\s*advantage|dynamic\s*asset|hybrid|savings|income|overnight|money\s*market|"
                   r"floater|duration|credit\s*risk|banking\s*(?:&|and)\s*psu|conservative|equity\s*savings|multi[\s-]*asset")
_LARGE = re.compile(r"(?i)large\s*cap|bluechip|blue\s*chip|top\s*100|nifty\s*50\b(?!.*equal)|sensex|nifty\s*next\s*50|nifty\s*100|"
                    r"nifty\s*bees|nifty\s*50\s*bees|junior\s*bees|\bindex\b")


def fund_role(f: dict[str, Any]) -> str:
    """Which job a fund does in a portfolio — one fund per job is enough."""
    name, cat = str(f.get("name") or ""), str(f.get("category") or "").lower()
    if re.search(r"(?i)gold|silver|commodit", name):
        return "gold"
    if _INTL.search(name):
        return "intl"
    if cat.startswith(("debt", "hybrid")) or _DEBT.search(name):
        return "debt"
    low = name.lower()
    if re.search(r"(?i)large\s*(?:&|and)\s*mid", name) or "large_and_mid" in cat or "large_mid" in cat:
        return "flexi"  # diversified across sizes, like a flexi / multi cap fund
    if "small" in low or cat.endswith("small_cap"):
        return "small"
    if ("mid" in low and "large" not in low) or cat.endswith("mid_cap"):
        return "mid"
    if (_LARGE.search(name) and "mid" not in low and "small" not in low) or cat.endswith(("large_cap", "index")):
        return "large"
    return "flexi"  # flexi / multi / focused / value / contra / large & mid / ELSS / sector & thematic


def fund_lineup(funds: dict[str, dict[str, Any]], pairs: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Too many funds: one fund per role (large / flexi / mid / small / debt / gold / international) — the best one you
    hold (``_keeper_rank``) — and every other fund in that role moves into it. Same shape as ``consolidation_plan``."""
    ov = {frozenset((p.get("a_id"), p.get("b_id"))): p["overlap_pct"] for p in pairs if p.get("a_id") and p.get("b_id")}
    by_role: dict[str, list[str]] = {}
    for i, f in funds.items():
        by_role.setdefault(fund_role(f), []).append(i)
    groups = []
    for role in ROLES:
        ids = sorted(by_role.get(role, []), key=lambda i: _keeper_rank(funds[i]), reverse=True)
        if not ids:
            continue
        keep_id, keep = ids[0], funds[ids[0]]
        moves = []
        for i in ids[1:]:
            ex = funds[i]
            pct = ov.get(frozenset((keep_id, i)))
            moves.append({"id": i, "name": ex["name"], "value": ex.get("value"), "overlap_pct": pct or 0.0, "common": [], "common_count": None,
                          "why_keep": _why_keep(keep, ex), "role": ROLES[role], **move_out(ex, keep, today)})
        groups.append({"keep_id": keep_id, "keep": keep["name"], "keep_code": keep.get("code"), "keep_value": keep.get("value"), "keep_score": keep.get("score"),
                       "keep_reason": _keep_reason(keep, [funds[m["id"]] for m in moves]) if moves else "the only fund in this role",
                       "keep_verdict": keep.get("verdict"), "role": ROLES[role], "role_key": role, "moves": moves, "lineup": True,
                       "replace": lags(keep)})  # even the best fund you hold here lags: the role needs a new fund, not more money in this one
    return groups


_INDEX = re.compile(r"(?i)\bindex\b|nifty|sensex|\bbees\b|\betf\b")


def is_index(f: dict[str, Any]) -> bool:
    """Index funds and ETFs track the index by design — a low "beat the benchmark" score doesn't make them laggards."""
    return str(f.get("category") or "").endswith("index") or f.get("asset_type") == "etf" or bool(_INDEX.search(str(f.get("name") or "")))


def lags(f: dict[str, Any]) -> bool:
    """A fund to leave on its own merits: the advisor says sell / switch / trim, or (active funds only) its fund
    score is low. Index funds are never laggards for their score (same rule as the rulebook)."""
    if f.get("verdict") in ("SWITCH", "EXIT", "TRIM"):
        return True
    return not is_index(f) and f.get("score") is not None and f["score"] < LAGGARD_SCORE


def into_of(g: dict[str, Any], own: bool = False) -> str:
    """Where this role's new money goes: the kept fund — or, when even that one lags, its replacement.
    ``own``: the text is for the lagging kept fund's own card (which lists the switch options itself)."""
    if g.get("into_name"):
        return g["into_name"]
    if not g.get("replace"):
        return g["keep"]
    role = g.get("role") or "fund"
    return f"the best-ranked {role} fund (see the switch options on this card)" if own else f"a better {role} fund (see the {g['keep']} card)"


LTCG_EXEMPT = 125_000  # equity long-term gains tax-free per financial year
LAGGARD_SCORE = 45
TINY_SHARE = 0.01       # under 1 % of the fund money (or ₹10,000): only clutter


def fund_plan(funds: dict[str, dict[str, Any]], pairs: list[dict[str, Any]], today: date,
              ltcg_left: float = LTCG_EXEMPT) -> list[dict[str, Any]]:
    """One method for every person, whatever the number of funds: one fund per role (``fund_lineup``; overlap is only
    a reason shown), then phased like a person would actually do it (``phase``): stop SIPs first (no tax), sell only
    laggards and tiny leftovers within what is left of this year's tax-free long-term gain, and simply hold the rest.
    (The engine runs the two steps itself, so a lagging role is re-targeted before the steps are written.)"""
    return phase(fund_lineup(funds, pairs, today), funds, today, ltcg_left)


def sip_active(lots: list[dict[str, Any]], today: date, estimated: bool = False) -> bool:
    """Is a SIP still running? The shared rule (``fm_common.sip``) — the same one Holdings uses for its SIP badge."""
    from fm_common.sip import sip_active as _active

    return _active(lots, today, estimated=estimated)


def _fy_end(today: date) -> date:
    return date(today.year + (today.month >= 4), 3, 31)


def phase(plan: list[dict[str, Any]], funds: dict[str, dict[str, Any]], today: date,
          ltcg_left: float = LTCG_EXEMPT) -> list[dict[str, Any]]:
    """Each fund to leave gets a mode:
    * ``exit``   — a laggard (low fund score, or the advisor already says sell / switch) or a tiny leftover: sell, but in
                   steps — long-term units now while this year's ₹1.25 lakh tax-free long-term gain lasts (laggards
                   first), the rest after 1 April, recent units once a year old, ELSS units as they unlock;
    * ``freeze`` — a decent fund that's merely redundant: don't sell (that only creates tax) — stop its SIP, send new money
                   to the kept fund; its share shrinks on its own.
    A role whose kept fund lags (``replace``) sells that fund too, into its replacement.
    ``ltcg_left``: this year's tax-free long-term gain still unused (the Tax page's figure). Each exit records the gain
    it books this financial year (``fy_ltcg``) so the Tax page doesn't offer the same allowance again."""
    from .report import inr

    total = sum(f.get("value") or 0 for f in funds.values()) or 1.0
    exits: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for g in plan:
        into = into_of(g)
        g.pop("keep_move", None)
        if g.get("replace") and g["keep_id"] in funds:  # the kept slot goes to a better fund: this one is sold over time too
            kf = funds[g["keep_id"]]
            g["keep_move"] = {"id": g["keep_id"], "name": g["keep"], "value": kf.get("value"), "overlap_pct": 0.0, "common": [], "common_count": None,
                              "why_keep": None, "role": g.get("role"), **move_out(kf, {"name": into_of(g, own=True)}, today)}
        for m in g["moves"] + ([g["keep_move"]] if g.get("keep_move") else []):
            f = funds.get(m["id"]) or {}
            laggard = lags(f) or m["id"] == g["keep_id"]
            # "too small to matter" is for old funds nobody invests in — a new SIP always starts small
            tiny = (f.get("value") or 0) < max(10_000.0, TINY_SHARE * total) and not f.get("sip_active")
            m["laggard"], m["tiny"] = laggard, tiny
            m["sip"] = bool(f.get("sip_active")) if "sip_active" in f else sip_active(f.get("lots") or [], today)
            m["mode"] = "exit" if laggard or tiny else "freeze"
            if m["mode"] == "freeze":
                m["do"] = ((f"Stop the SIP in {m['name']} and run it in {into} instead. " if m["sip"] else "Nothing to do — ")
                           + "keep the units: it's a decent fund and selling would only create tax. "
                           + f"Any new money for this kind of fund goes to {into}."
                           + (f" (If you still run a SIP in it, move it to {into}.)" if not m["sip"] and len(f.get("lots") or []) <= 1 else ""))
            else:
                exits.append((g, m))
    left = max(0.0, float(ltcg_left))
    for g, m in sorted(exits, key=lambda x: (not x[1]["laggard"], (funds.get(x[1]["id"]) or {}).get("score") or 0)):
        into = into_of(g, own=m["id"] == g["keep_id"])
        c = m["exit_cost"]
        m["fy_ltcg"] = 0.0
        long_value = c["value"] - c["locked_value"] - c["short_value"]
        steps = [f"Stop the SIP in {m['name']} now and start it in {into}."] if m.get("sip") else []
        if long_value > 0 and c["long_gain"] <= 0:
            steps.append(f"Sell the units held over a year ({inr(long_value)}) now and invest in {into} — they're at a loss, so no tax"
                         + (f", and the {inr(c['loss'])} loss is booked (it cuts tax on other gains, or carries forward 8 years)." if c["loss"] >= 100 else "."))
        elif long_value > 0:
            gain = c["long_gain"]
            if gain <= left:
                left -= gain
                m["fy_ltcg"] = round(gain, 2)
                steps.append(f"Sell the units held over a year ({inr(long_value)}) before {_fy_end(today):%d %b %Y} and invest in {into} — "
                             f"their {inr(gain)} gain fits in this year's ₹1.25 lakh tax-free long-term gains: tax ₹0.")
            else:
                part = left / gain if gain else 0
                if part > 0.05:
                    m["fy_ltcg"] = round(left, 2)
                    steps.append(f"Now: sell about {part:.0%} of the over-a-year units ({inr(long_value * part)}) — tax-free within this year's "
                                 f"₹1.25 lakh. After 1 April: sell the rest ({inr(long_value * (1 - part))}) under next year's allowance.")
                else:
                    steps.append(f"After 1 April: sell the over-a-year units ({inr(long_value)}) under next year's ₹1.25 lakh tax-free long-term gains"
                                 f" (this year's is used up by your other moves).")
                left = 0
        if c["short_value"]:
            steps.append(f"Units bought in the last year ({inr(c['short_value'])}): sell each as it turns 1 year old — avoids 20 % short-term tax and the exit load.")
        if c["locked_value"]:
            steps.append(f"ELSS units ({inr(c['locked_value'])}): sell as they unlock, from {c['unlock_from']}.")
        f = funds.get(m["id"]) or {}
        why = ("it's the regular plan — ≈0.5–1 %/yr more in commission than a direct plan" if m["laggard"] and f.get("regular")
               else "it lags (low fund score)" if m["laggard"] and f.get("score") is not None and f["score"] < LAGGARD_SCORE
               else "the advisor rates it a switch" if m["laggard"]
               else "it's too small to matter — only adds clutter")
        if m["id"] == g["keep_id"]:
            why = f"it's the best {g.get('role') or ''} fund you hold, but it lags too".replace("  ", " ")
        if not m.get("sip") and len((funds.get(m["id"]) or {}).get("lots") or []) <= 1:
            steps.append(f"(If you still run a SIP in it, move it to {into}.)")
        m["do"] = f"Sell over time — {why}. " + " ".join(steps)
    for g in plan:
        g["freeze"] = sum(1 for m in g["moves"] if m["mode"] == "freeze")
        g["into"] = into_of(g)
    return plan


def _phase_summary(plan: list[dict[str, Any]]) -> str:
    from .report import inr

    moves = [m for g in plan for m in g["moves"]]
    exits = [m for m in moves if m.get("mode") == "exit"]
    freeze = [m for m in moves if m.get("mode") == "freeze"]
    kept = [into_of(g) for g in plan]
    exits += [g["keep_move"] for g in plan if g.get("keep_move")]
    sips = [m["name"] for m in moves + [g["keep_move"] for g in plan if g.get("keep_move")] if m.get("sip")]
    parts = ["Step 1 (now, no tax): " + (f"move your SIP{'s' if len(sips) > 1 else ''} in {', '.join(sips)} to the kept funds. " if sips else "")
             + f"From now on put new money only into {', '.join(kept)}."]
    if exits:
        parts.append(f"Step 2 (over time, tax-planned): sell {len(exits)} fund{'s' if len(exits) > 1 else ''} that lag or are too small "
                     f"({inr(sum(m['exit_cost']['value'] for m in exits))}) — long-term units within the yearly ₹1.25 lakh tax-free gains, "
                     f"recent units after a year, ELSS after lock-in.")
    if freeze:
        parts.append(f"Step 3: just hold the other {len(freeze)} — decent old funds; selling them would only create tax. "
                     f"As new money goes to your kept funds, their share falls on its own.")
    return " ".join(parts)


def lookthrough_findings(profile: dict[str, Any], value: float, look: dict[str, Any] | None,
                         funds: dict[str, dict[str, Any]] | None = None, today: date | None = None,
                         plan: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Hidden concentration (a stock over your single-stock cap once your funds' holdings are counted) and
    redundant funds (two funds holding largely the same stocks)."""
    if (not look and not funds and not plan) or not value:
        return []
    look = look or {"stocks": [], "overlap": []}
    from .facts import SINGLE_CAP

    out: list[dict[str, Any]] = []
    cap = SINGLE_CAP.get(profile.get("risk_profile") or "moderate", SINGLE_CAP["moderate"])["stock"]
    for st in [s for s in look.get("stocks", []) if s["via_funds"] > 0][:20]:
        pct, direct_pct = st["total"] / value * 100, st["direct"] / value * 100
        if pct <= cap or direct_pct > cap:  # over the cap on its own is already a call on that stock
            continue
        excess = (pct - cap) / 100 * value
        via = ", ".join(f"{f['name']} ₹{f['value']:,.0f}" for f in st["funds"][:4])
        out.append({
            "rule_id": f"hidden_concentration:{st['isin']}", "action": "REBALANCE", "severity": 2, "confidence": 0.75, "impact": excess,
            "headline": f"{st['name']} is {pct:.0f}% of your money once your funds are counted (cap {cap:.0f}%)",
            "reasons": [f"Direct: ₹{st['direct']:,.0f} · through funds: ₹{st['via_funds']:,.0f} ({via})",
                        f"Single-stock cap for a {profile.get('risk_profile') or 'moderate'} profile: {cap:.0f}%",
                        "Fund holdings are from each fund house's latest monthly portfolio disclosure."],
            "evidence": {"stock": st, "cap_pct": cap},
            "what_would_change": f"Resolved when {st['name']} (direct + via funds) is ≤ {cap:.0f}% of the portfolio.",
            "do": (f"Don't add more {st['name']} directly; " if st["direct"] else "") +
                  f"direct new money to funds that hold less of it (about ₹{excess:,.0f} over the cap today).",
        })
        if len([f for f in out if f["rule_id"].startswith("hidden_concentration")]) >= 3:
            break
    if plan is None:
        plan = fund_plan(funds or {}, look.get("overlap", []), today or date.today()) if funds else []

    if any(g["moves"] for g in plan):  # some role has more than one fund
        roles = [g for g in plan]
        ids = [g["keep_id"] for g in plan] + [m["id"] for g in plan for m in g["moves"]]
        n_etf = sum(1 for i in ids if (funds or {}).get(i, {}).get("asset_type") == "etf")
        n_mf = len(ids) - n_etf
        counted = f"{n_mf} mutual fund{'s' if n_mf != 1 else ''}" + (f" + {n_etf} ETF{'s' if n_etf != 1 else ''}" if n_etf else "")
        out.append({
            "rule_id": "fund_lineup", "action": "SWITCH", "severity": 3, "confidence": 0.85,
            "impact": sum(m["move_value"] for g in plan for m in g["moves"]),
            "headline": f"{counted} — invest only in {len(roles)} from now on (one per role); no need to sell them all",
            "reasons": [f"{g['role']} ({1 + len(g['moves'])}): "
                        + (f"replace {g['keep']} (it lags) with {into_of(g)}" if g.get("replace") else f"keep {g['keep']}")
                        + (f"; also held: {', '.join(m['name'] for m in g['moves'])}" if g["moves"] else "") for g in roles]
                       + ["Beyond 4–6 funds, extra funds hold the same stocks again: more paperwork and costs, no extra diversification, "
                          "and returns drift towards the index." if len(ids) > LINEUP_MAX else
                          "Two funds doing the same job hold largely the same stocks — one is enough."],
            "evidence": {"lineup": plan}, "lineup": plan,
            "what_would_change": f"Resolved at {len(roles)} funds — one per role.",
            "do": _phase_summary(plan),
        })
    plan = [g for g in plan if g["moves"]]  # a role with one fund: its own card says keep / replace
    for g in plan:
        moving = sum(m["move_value"] for m in g["moves"])
        out.append({
            "rule_id": f"fund_consolidation:{g['keep_id']}"[:120], "action": "SWITCH", "severity": 2, "confidence": 0.8, "impact": moving,
            "headline": (f"{g['role'] + ': ' if g.get('lineup') else ''}"
                         + (f"even your best fund here lags — switch to a better one (options on the {g['keep']} card); "
                            if g.get("replace") and not g.get("into_name") else f"invest in {into_of(g)} only — ")
                         + f"{len(g['moves'])} other fund{'s' if len(g['moves']) > 1 else ''}: "
                         + ", ".join(x for x in (
                             f"sell {sum(1 for m in g['moves'] if m.get('mode') == 'exit')} over time" if any(m.get('mode') == 'exit' for m in g['moves']) else "",
                             f"hold {sum(1 for m in g['moves'] if m.get('mode') == 'freeze')}" if any(m.get('mode') == 'freeze' for m in g['moves']) else "",
                             f"move {sum(1 for m in g['moves'] if m.get('sip'))} SIP{'s' if sum(1 for m in g['moves'] if m.get('sip')) > 1 else ''}"
                             if any(m.get('sip') for m in g['moves']) else "") if x)),
            "reasons": [f"{m['name']} → {into_of(g)}: "
                        + (f"{m['overlap_pct']:.0f}% the same stocks" if m.get("overlap_pct") else f"same role ({g.get('role')})")
                        for m in g["moves"][:4]]
                       + [f"Why keep {g['keep']}: {g['keep_reason']}" if not g.get("replace") else
                          f"Every {g.get('role')} fund you hold lags — " + (f"the best {g.get('role')} fund right now is {g['into_name']}"
                                                                              if g.get("into_name") else f"replace {g['keep']} too (see its card)"),
                          "Funds doing the same job are one portfolio for several expense ratios — no extra diversification."],
            "evidence": {"plan": g}, "plan": g,
            "what_would_change": f"Resolved when you invest in one {g.get('role') or 'fund'} fund only.",
            "do": " ".join(f"{i + 1}) {m['name']}: {m['do']}" for i, m in enumerate(g["moves"][:4])),
        })
    if not funds:  # no per-fund facts (older callers): plain pairwise notes
        for pair in [p for p in look.get("overlap", []) if p["overlap_pct"] >= 50][:3]:
            common = ", ".join(c["name"] for c in pair["common"][:4])
            out.append({
                "rule_id": f"fund_overlap:{pair['a']}|{pair['b']}"[:120], "action": "REBALANCE", "severity": 2, "confidence": 0.75, "impact": 0.0,
                "headline": f"{pair['a']} and {pair['b']} hold largely the same stocks ({pair['overlap_pct']:.0f}% overlap)",
                "reasons": [f"{pair['common_count']} stocks in common — biggest shared: {common}",
                            "Two funds with this much overlap give you one portfolio for two expense ratios, with no extra diversification."],
                "evidence": {"overlap": pair}, "what_would_change": "Resolved when no two funds overlap by 50% or more.",
                "do": "Keep one of the two (usually the lower-cost / better-performing one); stop new SIPs in the other and "
                      "let it run down — mind exit loads and capital-gains tax before selling.",
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
