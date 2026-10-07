"""MF look-through maths (E16), shared by the exposure page (portfolio-svc) and the advisor.

* ``combine`` — true stock exposure: what you hold directly + your share of every stock inside your funds / ETFs
  (fund value × the stock's % of the fund's NAV, from the fund house's latest monthly portfolio).
* ``overlap`` — how much two funds hold the same stocks: Σ min(wA, wB) over their equity holdings, each fund's
  equity weights scaled to 100 % (the usual "portfolio overlap" measure; 100 % = identical stock portfolios).
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any

EQUITY = ("equity", "foreign_equity")
_SUFFIX = re.compile(r"\b(limited|ltd|the|co|company|corporation|corp|inc|plc|india)\b")


def company_key(name: str) -> str:
    """"HDFC Bank Ltd." / "HDFC BANK LIMITED" / "HDFC Bank" → "hdfc bank" — to line up a stock entered without an ISIN."""
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    return " ".join(_SUFFIX.sub(" ", n).split())


def unify(funds: list[dict[str, Any]], extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Holdings known only by name ("NAME:hdfc bank", from sources without ISINs) get the ISIN another source
    gave the same company — so a share counts once whichever source a fund's data came from."""
    known = dict(extra or {})
    for f in funds:
        for h in (f or {}).get("holdings") or []:
            if h.get("kind") in EQUITY and not str(h.get("isin", "")).startswith("NAME:"):
                known.setdefault(company_key(h["name"]), h["isin"])
    out = []
    for f in funds:
        hs = (f or {}).get("holdings")
        if not hs or not any(str(h.get("isin", "")).startswith("NAME:") for h in hs):
            out.append(f)
            continue
        out.append({**f, "holdings": [{**h, "isin": known.get(company_key(h["name"]), h["isin"])} if str(h.get("isin", "")).startswith("NAME:")
                                      else h for h in hs]})
    return out


def combine(positions: list[dict[str, Any]], funds: dict[str, dict[str, Any]], isin_of: dict[str, str]) -> dict[str, Any]:
    """positions: [{instrument_id, name, value, asset_type}] (one per instrument — merge family members first)
    funds: {instrument_id: market /funds/lookthrough entry} · isin_of: {instrument_id: ISIN} for direct stocks."""
    stocks: dict[str, dict[str, Any]] = {}
    covered, missing, stale = [], [], []
    fund_value = covered_value = 0.0

    direct_isins = {company_key(p["name"]): isin_of[str(p["instrument_id"])] for p in positions if str(p["instrument_id"]) in isin_of}
    keys = list(funds)
    funds = dict(zip(keys, unify([funds[k] for k in keys], direct_isins), strict=True))

    def slot(isin: str, name: str) -> dict[str, Any]:
        return stocks.setdefault(isin, {"isin": isin, "name": name, "direct": 0.0, "via_funds": 0.0, "funds": []})

    by_name = {company_key(h["name"]): h["isin"] for f in funds.values() for h in (f or {}).get("holdings") or [] if h.get("kind") in EQUITY}
    for p in positions:
        iid, v = str(p["instrument_id"]), float(p["value"] or 0)
        if v <= 0:
            continue
        if p["asset_type"] == "stock":
            isin = isin_of.get(iid) or by_name.get(company_key(p["name"]))  # no ISIN (entered by hand): match by company name
            if isin:
                slot(isin, p["name"])["direct"] += v
            continue
        if p["asset_type"] not in ("mutual_fund", "etf"):
            continue
        lt = funds.get(iid) or {}
        if lt.get("no_stocks"):  # liquid / debt / gold fund: nothing to look through, and nothing missing
            continue
        equity_holdings = [h for h in lt.get("holdings") or [] if h.get("kind") in EQUITY]
        if not lt.get("available") or not equity_holdings:
            if lt.get("available") is False or iid in funds:
                missing.append(p["name"])
            continue
        fund_value += v
        covered_value += v * sum(h["weight"] for h in equity_holdings) / 100
        covered.append({"name": p["name"], "scheme": lt.get("scheme"), "as_of": lt.get("as_of"), "stale": bool(lt.get("stale"))})
        if lt.get("stale"):
            stale.append(p["name"])
        for h in equity_holdings:
            share = v * h["weight"] / 100
            s = slot(h["isin"], h["name"])
            s["via_funds"] += share
            s["funds"].append({"name": p["name"], "value": round(share, 2), "weight": h["weight"]})
    out = []
    for s in stocks.values():
        total = s["direct"] + s["via_funds"]
        if total <= 0:
            continue
        out.append({**s, "direct": round(s["direct"], 2), "via_funds": round(s["via_funds"], 2), "total": round(total, 2),
                    "funds": sorted(s["funds"], key=lambda f: -f["value"])})
    out.sort(key=lambda s: -s["total"])
    return {"stocks": out, "covered": covered, "missing": sorted(set(missing)), "stale": sorted(set(stale)),
            "fund_value": round(fund_value, 2), "covered_equity_value": round(covered_value, 2)}


def _equity_weights(holdings: list[dict[str, Any]]) -> dict[str, float]:
    w = {h["isin"]: float(h["weight"]) for h in holdings if h.get("kind") in EQUITY and h.get("weight", 0) > 0}
    total = sum(w.values())
    return {k: v / total * 100 for k, v in w.items()} if total else {}


def overlap(funds: list[dict[str, Any]], min_pct: float = 0.0) -> list[dict[str, Any]]:
    """funds: [{name, holdings}] → [{a, b, overlap_pct, common: [{name, a_weight, b_weight}]}], highest first."""
    funds = unify(funds)
    prepared = [(f["name"], _equity_weights(f.get("holdings") or []), {h["isin"]: h["name"] for h in f.get("holdings") or []}, f.get("id"))
                for f in funds]
    prepared = [p for p in prepared if p[1]]
    out = []
    for (na, wa, names_a, ida), (nb, wb, _, idb) in combinations(prepared, 2):
        common = set(wa) & set(wb)
        pct = sum(min(wa[i], wb[i]) for i in common)
        if pct < min_pct:
            continue
        top = sorted(common, key=lambda i: -min(wa[i], wb[i]))[:5]
        out.append({"a": na, "b": nb, "a_id": ida, "b_id": idb, "overlap_pct": round(pct, 1), "common_count": len(common),
                    "common": [{"name": names_a.get(i, i), "a_weight": round(wa[i], 2), "b_weight": round(wb[i], 2)} for i in top]})
    return sorted(out, key=lambda x: -x["overlap_pct"])
