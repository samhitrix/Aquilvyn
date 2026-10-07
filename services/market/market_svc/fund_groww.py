"""Primary source for MF look-through: Groww's public fund pages (every scheme, one format, updated monthly).

Fund houses each publish their monthly portfolio on their own site in their own layout, and many of those
sites only work in a browser — so the per-fund-house scrape (``fund_sources``) breaks often. Groww shows
every scheme's full portfolio on its fund page and embeds it as JSON (``__NEXT_DATA__``):

    GET https://groww.in/v1/api/search/v3/query/global/st_query?query=<scheme name>&web=true
        → data.content[]: {scheme_code (AMFI code), search_id (page slug), title, …}
    GET https://groww.in/mutual-funds/<search_id>
        → props.pageProps.mfServerSideData.holdings[]:
          {company_name, nature_name (EQUITY / DEBT / CASH …), sector_name, corpus_per (% of NAV), portfolio_date}

Not a published API (it's what Groww's own web pages load), so it is read tolerantly and, when it fails,
the fund house's own file is used instead. Holdings carry company names, not ISINs: names are mapped to
ISINs through NSE's equity list so they line up with shares held directly and with fund-house files.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from fm_common.lookthrough import company_key

from .fund_portfolio import _PLAN_WORDS, match_score
from .fund_sources import Fetch

SEARCH = "https://groww.in/v1/api/search/v3/query/global/st_query?query={q}&web=true"
PAGE = "https://groww.in/mutual-funds/{slug}"
NEXT_DATA = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.S)
IST = timedelta(hours=5, minutes=30)


def search_queries(name: str) -> list[str]:
    """The full name, then without plan / option words (Groww's search ranks those poorly)."""
    trimmed = " ".join(w for w in re.split(r"[\s\-]+", name) if w and w.lower() not in _PLAN_WORDS)
    return [name] + ([trimmed] if trimmed and trimmed.lower() != name.lower() else [])


def pick_slug(payload: Any, code: str, name: str, amc: str = "") -> str | None:
    """Exact AMFI code first (either plan), else the best name match (direct / regular share one portfolio)."""
    items = ((payload or {}).get("data") or {}).get("content") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    funds = [i for i in items if isinstance(i, dict) and (i.get("search_id") or i.get("id"))
             and str(i.get("entity_type", "scheme")).lower() in ("scheme", "mutual_fund", "mf", "")]
    for i in funds:
        if code in {str(i.get(k)) for k in ("scheme_code", "direct_scheme_code", "regular_scheme_code", "amfi_code")}:
            return str(i.get("search_id") or i.get("id"))
    scored = [(match_score(str(i.get("title") or i.get("scheme_name") or i.get("search_id")).replace("-", " "), name, amc), i) for i in funds]
    best = max(scored, key=lambda x: x[0], default=(0.0, None))
    return str(best[1].get("search_id") or best[1].get("id")) if best[1] and best[0] >= 0.8 else None


def kind_of(nature: str, instrument: str = "") -> str:
    n = f"{nature} {instrument}".lower()
    if "equity" in n or re.search(r"\beq\b", n):
        return "equity"
    if "mutual" in n or "fund" in n or "etf" in n:
        return "fund"
    if "debt" in n or "bond" in n or "gilt" in n or "sovereign" in n or "t-bill" in n:
        return "debt"
    return "other"


def _as_of(holdings: list[dict[str, Any]]) -> date | None:
    dates = []
    for h in holdings:
        v = str(h.get("portfolio_date") or "")
        try:  # "2026-07-30T18:30:00.000Z" is midnight 31 Jul in India
            dates.append((datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC) + IST).date())
        except ValueError:
            continue
    return max(dates) if dates else None


def parse_page(html: str, name_to_isin: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Groww fund page → the same scheme dict ``fund_portfolio.parse_portfolio`` produces (None if no holdings)."""
    m = NEXT_DATA.search(html)
    if not m:
        return None
    try:
        mf = json.loads(m.group(1))["props"]["pageProps"]["mfServerSideData"]
    except (ValueError, KeyError, TypeError):
        return None
    raw = [h for h in mf.get("holdings") or [] if isinstance(h, dict) and h.get("company_name")]
    holdings = []
    for h in raw:
        try:
            w = float(h.get("corpus_per") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        name = str(h["company_name"]).strip()
        kind = kind_of(str(h.get("nature_name") or ""), str(h.get("instrument_name") or ""))
        key = company_key(name)
        isin = (name_to_isin or {}).get(key) if kind == "equity" else None
        holdings.append({"isin": isin or f"NAME:{key}", "name": name, "industry": str(h.get("sector_name") or ""), "kind": kind,
                         "weight": round(w, 4)})
    if not holdings:
        return None
    equity = sum(h["weight"] for h in holdings if h["kind"] == "equity")
    return {"sheet": "groww", "scheme": str(mf.get("scheme_name") or mf.get("fund_name") or ""), "titles": [],
            "as_of": _as_of(raw), "holdings": holdings, "equity_pct": round(equity, 2),
            "total_pct": round(sum(h["weight"] for h in holdings), 2)}


async def fetch_scheme(code: str, name: str, amc: str, fetch: Fetch, name_to_isin: dict[str, str] | None = None) -> dict[str, Any]:
    """→ {ok, scheme, url, error, tried, unreachable}"""
    tried: list[dict[str, Any]] = []
    out: dict[str, Any] = {"ok": False, "scheme": None, "url": None, "error": None, "tried": tried, "unreachable": False}
    slug = None
    try:
        for q in search_queries(name):
            url = SEARCH.format(q=quote(q))
            status, body, _f, _c = await fetch(url)
            tried.append({"step": "groww search", "url": url, "status": status})
            if status >= 500 or status in (0, 403, 429):
                out.update(unreachable=True, error=f"Groww search answered HTTP {status}")
                return out
            if status >= 400:
                continue
            try:
                slug = pick_slug(json.loads(body.decode("utf-8", "replace")), code, name, amc)
            except ValueError:
                slug = None
            if slug:
                break
        if not slug:
            out["error"] = f"Groww search found no fund page for {name}"
            return out
        url = PAGE.format(slug=slug)
        status, body, final, _c = await fetch(url)
        tried.append({"step": "groww page", "url": url, "status": status})
        if status >= 400:
            out["error"] = f"Groww fund page answered HTTP {status}"
            return out
        sc = parse_page(body.decode("utf-8", "replace"), name_to_isin)
        if not sc:
            out["error"] = "Groww fund page had no holdings (page layout may have changed)"
            return out
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        out.update(error=f"{type(exc).__name__}: {str(exc)[:160]}", unreachable=not tried)
        return out
    out.update(ok=True, scheme=sc, url=final)
    return out
