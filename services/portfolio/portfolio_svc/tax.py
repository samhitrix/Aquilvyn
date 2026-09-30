"""Tax engine: one person's capital-gains and investment-income picture for a financial year.

Rules (Finance (No. 2) Act 2024; sales on/after 23-Jul-2024 use the new rates):
  * listed equity / equity MF: short-term (≤ 12 months) 20 % (15 % before), long-term 12.5 % (10 % before)
    on gains above ₹1.25 lakh per FY (₹1 lakh up to FY 2023-24).
  * intraday equity = speculative business income at the slab rate; its losses only offset speculative gains.
  * debt ETFs / debt MFs (bought after Mar-2023): slab rate whatever the period.
  * other listed assets (gold ETF, "non-equity"): short-term at slab, long-term 12.5 %.
  * F&O: non-speculative business income at the slab rate.
  * dividends and interest: slab rate.
Set-off: short-term capital losses offset any capital gain, long-term losses only long-term gains;
leftovers carry forward 8 years (speculative: 4). Losses go first against the most heavily taxed gains.
Health & education cess 4 %. Surcharge is not modelled (it applies above ₹50 lakh of income).

This covers only what FolioSense sees (investments) — not salary, rent, TDS or deductions — so it
is an estimate to plan with, not a return to file."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

REGIME_CHANGE = date(2024, 7, 23)
CESS = 0.04
DEFAULT_SLAB = 30.0
ADVANCE_TAX = ((6, 15, 15), (9, 15, 45), (12, 15, 75), (3, 15, 100))  # (month, day, cumulative %)


def fy_of(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def fy_start(fy: str) -> date:
    return date(int(fy[:4]), 4, 1)


def exemption(fy: str) -> float:
    return 125_000.0 if int(fy[:4]) >= 2024 else 100_000.0


def _rates(sell: date | None, fy: str) -> tuple[float, float]:
    on = sell or (fy_start(fy) + timedelta(days=364))
    return (20.0, 12.5) if on >= REGIME_CHANGE else (15.0, 10.0)


def effective_gain(g: dict[str, Any]) -> float:
    """Taxable profit, using the cost the person entered when the statement showed a buy value of 0."""
    if g.get("cost_override") is not None:
        return float(g["sell_value"]) - float(g["cost_override"])
    return float(g["taxable_profit"])


def compute(fy: str, gains: list[dict[str, Any]], income: list[dict[str, Any]], slab_pct: float | None,
            today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    slab = float(slab_pct) if slab_pct is not None else DEFAULT_SLAB
    # ---- gross buckets (tax rate attached per bucket; equity rates can differ within FY 2024-25)
    b: dict[str, float] = {k: 0.0 for k in ("st_eq_new", "st_eq_old", "lt_eq_new", "lt_eq_old", "st_slab", "lt_other", "spec", "fno")}
    realised = 0.0
    for g in gains:
        p = effective_gain(g)
        realised += p
        st_rate, _ = _rates(g.get("sell_date"), fy)
        new = st_rate == 20.0
        asset, term = g["asset"], g["term"]
        if asset == "fno" or term == "business":
            b["fno"] += p
        elif term == "intraday":
            b["spec"] += p
        elif asset in ("equity", "equity_mf"):
            b[("lt_eq_" if term == "long" else "st_eq_") + ("new" if new else "old")] += p
        elif asset in ("debt", "debt_mf"):
            b["st_slab"] += p
        else:
            b["lt_other" if term == "long" else "st_slab"] += p
    gross = dict(b)
    dividends = sum(float(i["amount"]) for i in income if i["kind"] == "dividend")
    interest = sum(float(i["amount"]) for i in income if i["kind"] == "interest")

    # ---- set-off: losses first against the most heavily taxed gains
    st_keys = ["st_slab", "st_eq_new", "st_eq_old"]
    lt_keys = ["lt_other", "lt_eq_new", "lt_eq_old"]
    stcl = -sum(min(0.0, b[k]) for k in st_keys)
    ltcl = -sum(min(0.0, b[k]) for k in lt_keys)
    for k in st_keys + lt_keys:
        b[k] = max(0.0, b[k])
    order_st = sorted(st_keys, key=lambda k: -(slab if k == "st_slab" else 20.0 if k.endswith("new") else 15.0)) + lt_keys
    for k in order_st:
        use = min(stcl, b[k])
        b[k] -= use
        stcl -= use
    for k in lt_keys:
        use = min(ltcl, b[k])
        b[k] -= use
        ltcl -= use
    spec_loss_cf = max(0.0, -b["spec"])
    b["spec"] = max(0.0, b["spec"])
    fno_loss = max(0.0, -b["fno"])
    b["fno"] = max(0.0, b["fno"])

    # ---- LTCG exemption on listed equity (new-rate gains first: they're taxed higher)
    ex = exemption(fy)
    lt_eq = b["lt_eq_new"] + b["lt_eq_old"]
    used_ex = min(ex, lt_eq)
    left = used_ex
    taxable_lt_new = b["lt_eq_new"] - min(left, b["lt_eq_new"])
    left -= min(left, b["lt_eq_new"])
    taxable_lt_old = b["lt_eq_old"] - min(left, b["lt_eq_old"])

    lines = [
        {"key": "st_equity", "label": "Short-term gains · shares & equity funds", "gross": gross["st_eq_new"] + gross["st_eq_old"],
         "taxable": b["st_eq_new"] + b["st_eq_old"], "rate": "20%" if not gross["st_eq_old"] else "20% / 15%",
         "tax": b["st_eq_new"] * 0.20 + b["st_eq_old"] * 0.15},
        {"key": "lt_equity", "label": "Long-term gains · shares & equity funds", "gross": gross["lt_eq_new"] + gross["lt_eq_old"],
         "taxable": taxable_lt_new + taxable_lt_old, "rate": f"12.5% above ₹{ex / 100000:.2f} L",
         "tax": taxable_lt_new * 0.125 + taxable_lt_old * 0.10},
        {"key": "slab_gains", "label": "Debt funds / debt ETFs / other short-term", "gross": gross["st_slab"], "taxable": b["st_slab"],
         "rate": f"slab {slab:.0f}%", "tax": b["st_slab"] * slab / 100},
        {"key": "lt_other", "label": "Other long-term (gold ETF etc.)", "gross": gross["lt_other"], "taxable": b["lt_other"], "rate": "12.5%",
         "tax": b["lt_other"] * 0.125},
        {"key": "intraday", "label": "Intraday (speculative)", "gross": gross["spec"], "taxable": b["spec"], "rate": f"slab {slab:.0f}%",
         "tax": b["spec"] * slab / 100},
        {"key": "fno", "label": "F&O (business income)", "gross": gross["fno"], "taxable": b["fno"], "rate": f"slab {slab:.0f}%",
         "tax": b["fno"] * slab / 100},
        {"key": "dividends", "label": "Dividends", "gross": dividends, "taxable": dividends, "rate": f"slab {slab:.0f}%", "tax": dividends * slab / 100},
        {"key": "interest", "label": "Interest", "gross": interest, "taxable": interest, "rate": f"slab {slab:.0f}%", "tax": interest * slab / 100},
    ]
    lines = [{**x, "gross": round(x["gross"], 2), "taxable": round(x["taxable"], 2), "tax": round(x["tax"], 2)}
             for x in lines if abs(x["gross"]) > 0.004 or x["key"] in ("st_equity", "lt_equity", "dividends")]
    base_tax = sum(x["tax"] for x in lines)
    total = round(base_tax * (1 + CESS), 2)

    fy_end = date(int(fy[:4]) + 1, 3, 31)
    return {
        "fy": fy, "slab_pct": slab, "slab_assumed": slab_pct is None,
        "realised": round(realised, 2), "dividends": round(dividends, 2), "interest": round(interest, 2),
        "lines": lines, "tax_before_cess": round(base_tax, 2), "cess": round(base_tax * CESS, 2), "estimated_tax": total,
        "ltcg_exemption": {"limit": ex, "used": round(used_ex, 2), "left": round(ex - used_ex, 2)},
        "carry_forward": {"short_term_loss": round(stcl, 2), "long_term_loss": round(ltcl, 2), "speculative_loss": round(spec_loss_cf, 2),
                          "fno_loss": round(fno_loss, 2)},
        "advance_tax": advance_tax(fy, total, today) if today <= fy_end else None,
    }


def advance_tax(fy: str, total: float, today: date) -> dict[str, Any] | None:
    """Next instalment for the investment-income part of the tax. Capital gains and dividends only
    count from the instalment after they arise, so paying in the next one avoids interest (234C)."""
    y = int(fy[:4])
    for m, d, pct in ADVANCE_TAX:
        due = date(y + 1 if m == 3 else y, m, d)
        if due >= today:
            return {"due_date": due.isoformat(), "cumulative_pct": pct, "amount_by_then": round(total * pct / 100, 2),
                    "note": "Only the tax on investment income FolioSense sees — add salary / other income, minus TDS already deducted."}
    return None


MIN_GAIN_HARVEST = 1_000.0
TYPE_LABEL = {"stock": "Stocks", "mutual_fund": "Mutual funds", "etf": "ETFs"}


def _equity_like(h: dict[str, Any]) -> bool:
    meta = h.get("meta") or {}
    if h["asset_type"] == "stock":
        return True
    if h["asset_type"] == "etf":
        return "gold" not in str(h.get("name") or "").lower() and "liquid" not in str(h.get("name") or "").lower()
    return str(meta.get("mf_category") or "equity").startswith(("equity", "hybrid:aggressive"))


def _fifo_units(qty: float, whole: bool) -> float:
    return float(int(qty)) if whole else round(qty, 3)


def _best_loss_prefix(h: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Selling always uses the OLDEST units first (FIFO, as in a demat account). Find how many units to
    sell so the booked result is the biggest loss: walk the lots oldest-first and keep the most negative
    running total."""
    price = float(h["price"])
    whole = h["asset_type"] in ("stock", "etf")
    run = st = lt = 0.0
    units = 0.0
    best: dict[str, Any] | None = None
    for lot in h["lots"]:
        q = float(lot["qty"])
        g = (price - float(lot["cost"])) * q
        is_lt = date.fromisoformat(lot["buy_date"]) <= today - timedelta(days=365)
        run += g
        units += q
        st, lt = (st, lt + g) if is_lt else (st + g, lt)
        if run < -0.5 and (best is None or run < best["booked"]):
            best = {"quantity": _fifo_units(units, whole), "booked": round(run, 2), "st": round(st, 2), "lt": round(lt, 2)}
    return best


def _gain_prefix(h: dict[str, Any], room: float, today: date) -> dict[str, Any] | None:
    """Long-term units (oldest first) whose gain fits in the remaining tax-free room."""
    price = float(h["price"])
    whole = h["asset_type"] in ("stock", "etf")
    units = booked = 0.0
    for lot in h["lots"]:
        if date.fromisoformat(lot["buy_date"]) > today - timedelta(days=365):
            break  # the next unit sold would be short-term (taxed): stop here
        per = price - float(lot["cost"])
        q = float(lot["qty"])
        if per <= 0:
            units += q
            booked += per * q
            continue
        take = min(q, max(0.0, room - booked) / per)
        take = float(int(take)) if whole else round(take, 3)
        units += take
        booked += per * take
        if take < q:
            break
    if units <= 0 or booked <= 0.5:
        return None
    return {"quantity": _fifo_units(units, whole), "booked": round(booked, 2)}


def harvest(fy: str, summary: dict[str, Any], holdings_rows: list[dict[str, Any]], today: date | None = None,
            done: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Concrete tax-harvesting actions from current holdings, each with the units to sell and the tax it saves.

    * loss harvesting — booking a loss offsets this year's taxable gains: short-term losses go against
      slab-taxed gains, then short-term equity (20%), then long-term (12.5%); long-term losses only against
      long-term gains. Savings are allocated in order, so they never double-count the same taxable rupee.
    * gain harvesting — while the ₹1.25 L long-term exemption has room: sell and re-buy to book gains tax-free.
    ``done``: actions the person marked as done (not yet in an imported statement) — their effect is applied first.
    """
    today = today or date.today()
    slab = float(summary["slab_pct"])
    line = {x["key"]: x for x in summary["lines"]}
    pools = {"slab": line.get("slab_gains", {}).get("taxable", 0.0), "st_eq": line.get("st_equity", {}).get("taxable", 0.0),
             "lt_other": line.get("lt_other", {}).get("taxable", 0.0), "lt_eq": line.get("lt_equity", {}).get("taxable", 0.0)}
    rate = {"slab": slab, "st_eq": 20.0, "lt_other": 12.5, "lt_eq": 12.5}
    room = summary["ltcg_exemption"]["left"]

    def offset(st_loss: float, lt_loss: float, apply: bool) -> float:
        """Tax saved (incl. cess) by booking these losses (positive numbers)."""
        p = dict(pools)
        saved = 0.0
        for k in ("slab", "st_eq", "lt_other", "lt_eq"):
            use = min(st_loss, p[k])
            p[k] -= use
            st_loss -= use
            saved += use * rate[k] / 100
        for k in ("lt_other", "lt_eq"):
            use = min(lt_loss, p[k])
            p[k] -= use
            lt_loss -= use
            saved += use * rate[k] / 100
        if apply:
            pools.update(p)
        return round(saved * (1 + CESS), 2)

    done = done or []
    done_keys = {d["key"] for d in done if not d.get("reflected")}
    for d in done:
        if d.get("reflected"):
            continue  # already inside the imported statement's figures
        if d["kind"] == "loss":
            offset(max(0.0, -float(d.get("st_part") or 0)), max(0.0, -float(d.get("lt_part") or 0)), apply=True)
        else:
            room = max(0.0, room - float(d.get("booked") or 0))

    losses: list[dict[str, Any]] = []
    gains: list[dict[str, Any]] = []
    for h in holdings_rows:
        at = h.get("asset_type")
        if at not in TYPE_LABEL or not h.get("price") or not h.get("lots"):
            continue
        base = {"instrument_id": h.get("instrument_id"), "symbol": h["symbol"], "name": h.get("name") or h["symbol"], "profile_id": h.get("profile_id"),
                "type": at, "type_label": TYPE_LABEL[at], "price": float(h["price"]), "dates_estimated": bool(h.get("dates_estimated")),
                "equity_like": _equity_like(h)}
        b = _best_loss_prefix(h, today)
        if b and f"loss:{h.get('instrument_id')}" not in done_keys:
            st_p, lt_p = b["st"], b["lt"]
            if st_p > 0:  # a short-term gain in the same sale nets against its long-term loss (and vice versa)
                st_p, lt_p = 0.0, lt_p + st_p
            elif lt_p > 0:
                st_p, lt_p = st_p + lt_p, 0.0
            losses.append({**base, "key": f"loss:{h.get('instrument_id')}", "kind": "loss", "quantity": b["quantity"],
                           "value": round(b["quantity"] * float(h["price"]), 2), "booked": b["booked"],
                           "st_part": round(min(0.0, st_p), 2), "lt_part": round(min(0.0, lt_p), 2)})
        if base["equity_like"] and f"gain:{h.get('instrument_id')}" not in done_keys:
            g = _gain_prefix(h, 1e12, today)
            if g:
                gains.append({**base, "key": f"gain:{h.get('instrument_id')}", "kind": "gain", "max_booked": g["booked"]})
    # losses: biggest saving first, allocated against what is left to offset
    losses.sort(key=lambda x: x["booked"])
    ranked = sorted(losses, key=lambda x: -offset(-x["st_part"], -x["lt_part"], apply=False))
    for x in ranked:
        x["tax_saved"] = offset(-x["st_part"], -x["lt_part"], apply=True)
    # gains: fill the remaining tax-free room, biggest first
    gains.sort(key=lambda x: -x["max_booked"])
    gain_actions = []
    for x in gains:
        if room <= 0.5:
            break
        h = next(r for r in holdings_rows if r.get("instrument_id") == x["instrument_id"] and r.get("profile_id") == x["profile_id"])
        g = _gain_prefix(h, room, today)
        if not g or g["booked"] < MIN_GAIN_HARVEST:
            continue  # not worth a sell + re-buy (costs, effort)
        room -= g["booked"]
        tax_saved_later = round(g["booked"] * 0.125 * (1 + CESS), 2)
        gain_actions.append({**{k: v for k, v in x.items() if k != "max_booked"}, "quantity": g["quantity"],
                             "value": round(g["quantity"] * x["price"], 2), "booked": g["booked"], "tax_saved": tax_saved_later})
    actions = [x for x in ranked if x["tax_saved"] > 0] + gain_actions + [x for x in ranked if x["tax_saved"] <= 0]
    return {
        "ltcg_headroom": summary["ltcg_exemption"]["left"], "room_left": round(room, 2), "by": date(int(fy[:4]) + 1, 3, 31).isoformat(),
        "actions": actions, "total_saving": round(sum(x["tax_saved"] for x in actions if x["kind"] == "loss"), 2),
        "future_saving": round(sum(x["tax_saved"] for x in actions if x["kind"] == "gain"), 2),
        "taxable_left": {k: round(v, 2) for k, v in pools.items()},
    }


def money(v: Decimal | float | None) -> float:
    return round(float(v or 0), 2)
