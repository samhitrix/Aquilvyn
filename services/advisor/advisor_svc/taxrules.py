"""Indian capital-gains rules used by the Tax-Aware Timing Engine (E22, P1 hints).

Finance (No. 2) Act 2024, effective 23-Jul-2024:
  * listed equity / equity MF / equity ETF / listed REIT-InvIT units: long-term after 12 months;
    STCG 20 % (15 % before), LTCG 12.5 % (10 % before) above ₹1.25 lakh/FY (₹1 lakh before).
  * debt MFs bought on/after 1-Apr-2023: always taxed at slab (no LTCG benefit).
  * other assets (unlisted, gold MFs/FoFs, physical gold): long-term after 24 months, LTCG 12.5 %.
Full FY capital-gains reports (grandfathering, set-offs, ITR schedules) arrive with tax-svc in P2."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

REGIME_CHANGE = date(2024, 7, 23)
DEBT_MF_CUTOFF = date(2023, 4, 1)


def equity_like(asset_type: str, meta: dict[str, Any]) -> bool:
    if asset_type in ("stock", "reit"):
        return True
    if asset_type == "etf":
        return "gold" not in str(meta.get("name", "")).lower()
    if asset_type == "mutual_fund":
        return str(meta.get("mf_category", "equity")).startswith(("equity", "hybrid:aggressive"))
    return False


def rates(on: date) -> dict[str, float]:
    new = on >= REGIME_CHANGE
    return {"stcg": 20.0 if new else 15.0, "ltcg": 12.5 if new else 10.0, "ltcg_exemption": 125_000.0 if new else 100_000.0}


def lt_threshold_days(asset_type: str, meta: dict[str, Any], buy_date: date) -> int | None:
    if asset_type == "mutual_fund" and not equity_like(asset_type, meta) and buy_date >= DEBT_MF_CUTOFF:
        return None  # always slab
    if equity_like(asset_type, meta) or asset_type in ("etf", "gold"):
        return 365
    if asset_type in ("epf", "vpf", "ppf", "nps", "fixed_deposit", "cash"):
        return None  # EEE / interest income — not capital gains
    return 730


def timing(row: dict[str, Any], meta: dict[str, Any], slab_pct: float | None, today: date | None = None) -> dict[str, Any]:
    """Per-holding tax picture from open lots: which lots are ST with gains, days until they turn
    LT, and the tax saved by waiting; plus harvestable LT gains."""
    today = today or date.today()
    price = row.get("price")
    if price is None or not row.get("lots"):
        return {"applicable": False}
    r = rates(today)
    slab = float(slab_pct if slab_pct is not None else 30.0)
    st_gain = lt_gain = 0.0
    soonest: tuple[int, float] | None = None
    lots_out = []
    for lot in row["lots"]:
        bd = date.fromisoformat(lot["buy_date"])
        gain = (price - lot["cost"]) * lot["qty"]
        thr = lt_threshold_days(row["asset_type"], meta, bd)
        if thr is None:
            lots_out.append({**lot, "term": "slab", "gain": round(gain, 2)})
            st_gain += gain
            continue
        lt_on = bd + timedelta(days=thr + 1)
        is_lt = today >= lt_on
        if is_lt:
            lt_gain += gain
        else:
            st_gain += gain
            days = (lt_on - today).days
            if gain > 0 and (soonest is None or days < soonest[0]):
                soonest = (days, gain)
        lots_out.append({**lot, "term": "long" if is_lt else "short", "long_term_on": lt_on.isoformat(), "gain": round(gain, 2)})
    eq = equity_like(row["asset_type"], meta)
    st_rate = r["stcg"] if eq else slab
    lt_rate = r["ltcg"]
    out: dict[str, Any] = {
        "applicable": True, "equity_taxation": eq, "short_term_gain": round(st_gain, 2), "long_term_gain": round(lt_gain, 2),
        "rates": {"short_term_pct": st_rate, "long_term_pct": lt_rate, "ltcg_exemption": r["ltcg_exemption"]},
        "est_tax_if_sold_now": round(max(st_gain, 0) * st_rate / 100 + max(lt_gain, 0) * lt_rate / 100, 2), "lots": lots_out,
    }
    if soonest:
        days, gain = soonest
        out["days_to_ltcg"] = days
        out["saving_if_wait"] = round(gain * (st_rate - lt_rate) / 100, 2)
    return out
