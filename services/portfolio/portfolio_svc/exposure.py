"""Portfolio analytics (exposure): asset class, product (stocks / ETFs / mutual funds / retirement),
sector, and market-cap size — for the whole family or one profile, on current value or invested.

Look-through for mutual funds is by *category* (a large-cap fund counts as large cap, a flexi-cap
fund as flexi/multi cap, sectors as "diversified via mutual funds") — fund-level portfolio
disclosures are not loaded in this phase, and the response says so.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from fm_common import http
from fm_common.config import settings
from fm_common.deps import Principal
from fm_common.logging import get_logger

from .ledger import asset_class

log = get_logger(__name__)

PRODUCT = {"stock": "Stocks", "etf": "ETFs", "mutual_fund": "Mutual funds", "epf": "Retirement (EPF/PPF/NPS)", "vpf": "Retirement (EPF/PPF/NPS)",
           "ppf": "Retirement (EPF/PPF/NPS)", "nps": "Retirement (EPF/PPF/NPS)", "fixed_deposit": "Deposits & bonds", "bond": "Deposits & bonds",
           "gold": "Gold", "reit": "REITs / InvITs"}
ASSET_LABEL = {"equity": "Equity", "debt": "Debt", "gold": "Gold", "hybrid": "Hybrid", "real_estate": "Real estate", "alternative": "Alternative",
               "cash": "Cash", "other": "Other"}
CAP_LABEL = {"large": "Large cap", "mid": "Mid cap", "small": "Small cap", "flexi": "Flexi / multi cap (funds)", "unknown": "Size not known yet"}


def _mf_cap(category: str) -> str | dict[str, float] | None:
    c = category or ""
    if not c.startswith("equity"):
        return None
    if "large_mid" in c:
        return {"large": 0.5, "mid": 0.5}
    for key, bucket in (("large_cap", "large"), ("mid_cap", "mid"), ("small_cap", "small"), ("index", "large")):
        if key in c:
            return bucket
    return "flexi"


def _etf_cap(name: str) -> str | None:
    n = name.lower()
    if "small" in n:
        return "small"
    if "mid" in n:
        return "mid"
    if any(k in n for k in ("nifty", "sensex", "junior", "next 50", "bank", "psu", "it bees", "50")):
        return "large"
    return None


def _slices(values: dict[str, float], total: float, labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out = [{"key": k, "label": (labels or {}).get(k, k), "value": round(v, 2), "pct": round(v / total * 100, 2) if total else 0.0}
           for k, v in values.items() if v > 0]
    return sorted(out, key=lambda x: -x["value"])


async def exposure(principal: Principal, rows: list[dict[str, Any]], *, basis: str = "current", lookthrough: bool = True,
                   asset: str = "all") -> dict[str, Any]:
    rows = [r for r in rows if r["quantity"] > 0 or r["asset_type"] in ("epf", "vpf", "ppf", "fixed_deposit", "bond", "cash")]
    val = (lambda r: r["invested"]) if basis == "invested" else (lambda r: r["market_value"])  # noqa: E731
    for r in rows:
        r["_class"] = asset_class(r, {"meta": r.get("meta") or {}})
    classes_all = _slices(_sum(rows, lambda r: r["_class"], val), sum(val(r) for r in rows), ASSET_LABEL)
    class_holdings = _class_holdings(rows, val)
    if asset != "all":
        rows = [r for r in rows if r["_class"] == asset]
    total = sum(val(r) for r in rows)

    ids = sorted({r["instrument_id"] for r in rows if r["asset_type"] == "stock"})
    info: dict[str, Any] = {}
    if ids:
        try:
            info = await http.post(settings.market_url, "/api/v1/market/instruments/classify", token=principal.token, json={"ids": ids})
        except Exception as exc:
            log.warning("exposure.classify_failed", error=str(exc))

    products = _sum(rows, lambda r: PRODUCT.get(r["asset_type"], "Other"), val)

    sectors: dict[str, float] = defaultdict(float)
    caps: dict[str, float] = defaultdict(float)
    holdings: list[dict[str, Any]] = []
    sector_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pending = 0
    for r in rows:
        v = val(r)
        name = r.get("name") or r["symbol"]
        if r["asset_type"] == "stock":
            i = info.get(r["instrument_id"]) or {}
            sec = i.get("sector") or r.get("sector") or "Unclassified"
            cap = i.get("cap") or "unknown"
            pending += bool(i.get("pending")) and cap == "unknown"
        elif r["asset_type"] == "etf":
            if r["_class"] != "equity":
                continue  # gold / liquid ETFs are not stock exposure
            sec, cap = "ETF", _etf_cap(name) or "unknown"
        elif r["asset_type"] == "mutual_fund" and lookthrough:
            mc = _mf_cap(str((r.get("meta") or {}).get("mf_category") or ""))
            if mc is None:
                continue  # debt / hybrid funds
            sec = "Diversified (via mutual funds)"
            if isinstance(mc, dict):
                for k, w in mc.items():
                    caps[k] += v * w
                sectors[sec] += v
                sector_members[sec].append({"name": name, "value": round(v, 2)})
                holdings.append({"name": name, "symbol": r["symbol"], "value": round(v, 2), "cap": "flexi", "kind": "fund"})
                continue
            cap = mc
        else:
            continue
        sectors[sec] += v
        caps[cap] += v
        sector_members[sec].append({"name": name, "value": round(v, 2)})
        holdings.append({"name": name, "symbol": r["symbol"], "value": round(v, 2), "cap": cap,
                         "kind": "fund" if r["asset_type"] == "mutual_fund" else r["asset_type"]})
    # the same instrument held by several family members is one line
    merged: dict[str, dict[str, Any]] = {}
    for h in holdings:
        m = merged.setdefault(h["symbol"], {**h, "value": 0.0})
        m["value"] = round(m["value"] + h["value"], 2)
    holdings = list(merged.values())
    for sec, members in sector_members.items():
        by_name: dict[str, float] = defaultdict(float)
        for m in members:
            by_name[m["name"]] += m["value"]
        sector_members[sec] = [{"name": n, "value": round(v, 2)} for n, v in by_name.items()]
    equity_total = sum(sectors.values())
    notes = []
    if pending:
        notes.append(f"Market size for {pending} stock(s) is being looked up — refresh in a minute.")
    if lookthrough:
        notes.append("Mutual funds are looked through by category (e.g. a large-cap fund counts as large cap); "
                     "their exact stock holdings are not loaded yet.")
    notes.append("Large / mid / small follow SEBI's bands (top 100 / 101–250 / rest by market cap), taken from NSE's Nifty 100 and "
                 "Nifty Midcap 150 lists; market cap is used only if those lists can't be reached.")
    unsectored = sorted({m["name"] for m in sector_members.get("Unclassified", [])})
    if unsectored:
        notes.append(f"No sector yet for: {', '.join(unsectored[:8])}{'…' if len(unsectored) > 8 else ''} — re-import your broker's holdings "
                     "statement (Sync) to take sectors from its Sector column.")
    unknown = [h["name"] for h in holdings if h["cap"] == "unknown"]
    if unknown and not pending:
        notes.append(f"Size unknown for: {', '.join(unknown[:8])}{'…' if len(unknown) > 8 else ''} — see Settings → Data sources "
                     "(NSE size lists) if this persists.")
    return {
        "basis": basis, "asset": asset, "lookthrough": lookthrough, "total": round(total, 2),
        "asset_classes": [{**c, "holdings": class_holdings.get(c["key"], [])} for c in classes_all],
        "products": _slices(products, total),
        "sectors": [{**s, "holdings": sorted(sector_members[s["key"]], key=lambda x: -x["value"])} for s in _slices(dict(sectors), equity_total)],
        "caps": _slices(dict(caps), equity_total, CAP_LABEL),
        "holdings": [{**h, "pct": round(h["value"] / equity_total * 100, 2) if equity_total else 0.0} for h in sorted(holdings, key=lambda x: -x["value"])],
        "equity_total": round(equity_total, 2), "notes": notes,
    }


def _kind(r: dict[str, Any]) -> str:
    """What the holding is, in words: "Mutual fund · liquid", "EPF", "ETF", "Stock" …"""
    t = r["asset_type"]
    if t == "mutual_fund":
        cat = str((r.get("meta") or {}).get("mf_category") or "").split(":")[-1].replace("_", " ").strip()
        return f"Mutual fund · {cat}" if cat else "Mutual fund"
    return {"stock": "Stock", "etf": "ETF", "epf": "EPF", "vpf": "VPF", "ppf": "PPF", "nps": "NPS", "fixed_deposit": "Fixed deposit",
            "bond": "Bond", "gold": "Gold", "reit": "REIT / InvIT", "cash": "Cash"}.get(t, t.replace("_", " ").title())


def _class_holdings(rows: list[dict[str, Any]], val: Any) -> dict[str, list[dict[str, Any]]]:
    """Every holding under each asset class (Debt: EPF, PPF, liquid fund …) — one line per instrument, with who holds it."""
    by: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        v = val(r)
        if v <= 0:
            continue
        m = by[r["_class"]].setdefault(r["symbol"], {"name": r.get("name") or r["symbol"], "symbol": r["symbol"], "kind": _kind(r),
                                                      "value": 0.0, "people": []})
        m["value"] += v
        if r.get("profile_name") and r["profile_name"] not in m["people"]:
            m["people"].append(r["profile_name"])
    out: dict[str, list[dict[str, Any]]] = {}
    for cls, items in by.items():
        total = sum(i["value"] for i in items.values()) or 1.0
        out[cls] = sorted(({**i, "value": round(i["value"], 2), "pct": round(i["value"] / total * 100, 2)} for i in items.values()),
                          key=lambda x: -x["value"])
    return out


def _sum(rows: list[dict[str, Any]], key: Any, val: Any) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        out[key(r)] += val(r)
    return dict(out)
