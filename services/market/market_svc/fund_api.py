"""Fallback source for MF look-through: a public holdings API (mfdata.in — free, no key).

Fund houses' own disclosure files stay the primary source (``fund_sources``). Several AMC sites build their
download lists with JavaScript, though, so a plain download finds nothing; for those schemes (and when a
fund house only offers an old file) we ask the API, which collects the same monthly disclosures.

    GET {base}/schemes/{amfi_code}            → the scheme, with its family id
    GET {base}/families/{family_id}/holdings  → the family's latest portfolio (direct / regular plans share it)

The response is read tolerantly (field names vary between versions); anything unreadable is reported with
the keys we saw, so `fm.py funds-check` output is enough to adjust this file.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from fm_common.lookthrough import company_key

from .fund_portfolio import isin_kind, month_end, parse_date_text
from .fund_sources import Fetch, load_sources

DEFAULT_BASE = "https://mfdata.in/api/v1"
NAME_KEYS = ("stock_name", "company_name", "security_name", "instrument_name", "name", "company", "security", "instrument", "holding_name")
WEIGHT_KEYS = ("weight_pct", "weight", "percentage", "percent", "pct", "pct_of_nav", "percent_of_nav", "holding_pct",
               "allocation", "corpus_pct", "nav_pct", "net_assets_pct")
INDUSTRY_KEYS = ("sector", "industry", "sector_name", "rating")
DATE_KEYS = ("month", "as_of", "as_on", "portfolio_date", "date", "period", "disclosure_date")


def api_base() -> str | None:
    """'' in fund_sources.json → API fallback switched off."""
    base = load_sources().get("holdings_api", DEFAULT_BASE)
    return base.rstrip("/") or None


def _unwrap(payload: Any) -> Any:
    while isinstance(payload, dict) and len(payload) <= 4 and isinstance(payload.get("data"), dict | list):
        payload = payload["data"]
    return payload


def find_key(obj: Any, names: tuple[str, ...], depth: int = 3) -> Any:
    """First value under any of ``names`` (breadth-first, a few levels deep)."""
    level = [obj]
    for _ in range(depth):
        nxt = []
        for o in level:
            if isinstance(o, dict):
                for n in names:
                    if o.get(n) not in (None, "", []):
                        return o[n]
                nxt += [v for v in o.values() if isinstance(v, dict | list)]
            elif isinstance(o, list):
                nxt += [v for v in o[:3] if isinstance(v, dict)]
        level = nxt
    return None


def family_id(payload: Any) -> str | None:
    p = _unwrap(payload)
    if isinstance(p, list):
        p = p[0] if p else {}
    fid = find_key(p, ("family_id", "familyId", "scheme_family_id"))
    if fid is None:
        fam = find_key(p, ("family",))
        fid = fam.get("id") if isinstance(fam, dict) else fam if isinstance(fam, int | str) else None
    return str(fid) if fid not in (None, "") else None


def _num(v: Any) -> float | None:
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        return float(m.group()) if m else None
    return None


def _as_of(p: Any) -> date | None:
    v = find_key(p, DATE_KEYS)
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"(20\d{2})-(\d{2})", v.strip())
    if m:
        return month_end(int(m.group(1)), int(m.group(2)))
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", v.strip())
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return parse_date_text(v + " as on")


def _holding_lists(p: Any, key: str = "", depth: int = 0) -> list[tuple[str, list[dict[str, Any]]]]:
    """Every list of holding-looking dicts, with the key it sat under ("equity_holdings", "debt", …)."""
    out: list[tuple[str, list[dict[str, Any]]]] = []
    if depth > 4:
        return out
    if isinstance(p, list) and p and all(isinstance(x, dict) for x in p[:5]):
        if any(any(k in x for k in WEIGHT_KEYS) for x in p[:5]):
            return [(key, p)]
    if isinstance(p, dict):
        for k, v in p.items():
            out += _holding_lists(v, k.lower(), depth + 1)
    return out


def normalise(payload: Any, scheme_name: str = "") -> dict[str, Any] | None:
    """API holdings payload → the same scheme dict ``fund_portfolio.parse_portfolio`` produces."""
    p = _unwrap(payload)
    holdings: list[dict[str, Any]] = []
    for key, rows in _holding_lists(p):
        for r in rows:
            w = next((_num(r[k]) for k in WEIGHT_KEYS if r.get(k) is not None), None)
            name = next((str(r[k]) for k in NAME_KEYS if r.get(k)), "")
            if w is None or not name:
                continue
            isin = str(r.get("isin") or r.get("isin_code") or "").strip().upper()
            industry = next((str(r[k]) for k in INDUSTRY_KEYS if r.get(k)), "")
            listed_as = str(r.get("type") or r.get("holding_type") or r.get("asset_type") or key).lower()
            if isin:
                kind = isin_kind(isin, industry)
            elif "equity" in listed_as or "stock" in listed_as:
                kind = "equity"
            elif "debt" in listed_as or "bond" in listed_as:
                kind = "debt"
            else:
                kind = "other"
            holdings.append({"isin": isin or f"NAME:{company_key(name)}", "name": name, "industry": industry, "kind": kind, "weight": w})
    if not holdings:
        return None
    if sum(h["weight"] for h in holdings) <= 1.5:  # fractions → %
        for h in holdings:
            h["weight"] *= 100
    for h in holdings:
        h["weight"] = round(h["weight"], 4)
    equity = sum(h["weight"] for h in holdings if h["kind"] in ("equity", "foreign_equity"))
    name = find_key(p, ("family_name", "scheme_name", "name")) if isinstance(p, dict) else None
    return {"sheet": "api", "scheme": str(name or scheme_name), "titles": [], "as_of": _as_of(p), "holdings": holdings,
            "equity_pct": round(equity, 2), "total_pct": round(sum(h["weight"] for h in holdings), 2)}


def _keys(payload: Any) -> str:
    p = _unwrap(payload)
    return ", ".join(list(p)[:12]) if isinstance(p, dict) else type(p).__name__


async def _get_json(url: str, fetch: Fetch) -> tuple[int, Any]:
    status, body, _final, _ctype = await fetch(url)
    if status >= 400:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return status, None


async def fetch_scheme(code: str, name: str, fetch: Fetch, base: str | None = None) -> dict[str, Any]:
    """→ {ok, scheme (as parse_portfolio), url, error, tried}"""
    base = base or api_base()
    tried: list[dict[str, Any]] = []
    out: dict[str, Any] = {"ok": False, "scheme": None, "url": None, "error": None, "tried": tried}
    if not base:
        out["error"] = "holdings API switched off (holdings_api is empty in fund_sources.json)"
        return out
    try:
        url = f"{base}/schemes/{code}"
        status, data = await _get_json(url, fetch)
        tried.append({"step": "api scheme", "url": url, "status": status})
        fid = family_id(data) if data is not None else None
        if status >= 500 or status == 0:
            out["unreachable"] = True
        if not fid:
            out["error"] = f"API has no family for scheme {code}" + (f" (fields: {_keys(data)})" if data is not None else f" (HTTP {status})")
            return out
        url = f"{base}/families/{fid}/holdings"
        status, data = await _get_json(url, fetch)
        tried.append({"step": "api holdings", "url": url, "status": status})
        sc = normalise(data, name) if data is not None else None
        if not sc:
            out["error"] = "API returned no readable holdings" + (f" (fields: {_keys(data)})" if data is not None else f" (HTTP {status})")
            return out
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        out["unreachable"] = not tried
        return out
    out.update(ok=True, scheme=sc, url=url)
    return out
