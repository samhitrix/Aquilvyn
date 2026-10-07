"""NSE's own quote API — a second source for basic company data when Yahoo has none.

``/api/quote-equity?symbol=X`` gives the company name, industry classification, the stock's P/E and its
sector's P/E, issued shares (→ market cap) and 52-week range. NSE only answers browser-like requests that
first loaded the site (cookies), so a small shared session does that once.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fm_common.logging import get_logger

from . import browser

log = get_logger(__name__)
_primed = 0.0
_lock = asyncio.Lock()
_HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9", "Referer": "https://www.nseindia.com/"}


async def _prime(force: bool = False) -> None:
    """NSE only answers API calls from a session that loaded the site first (cookies) — refreshed every 10 min."""
    global _primed
    async with _lock:
        if force or time.monotonic() - _primed > 600:
            if force:
                browser.reset("nse")
            await browser.get("nse", "https://www.nseindia.com/", base_headers=_HEADERS)
            _primed = time.monotonic()


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", ""))
        return f if f == f else None
    except (TypeError, ValueError):
        return None


async def fundamentals(symbol: str) -> dict[str, Any] | None:
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    if not base or base.startswith("^"):
        return None
    await _prime()
    url = "https://www.nseindia.com/api/quote-equity"
    r = await browser.get("nse", url, params={"symbol": base}, base_headers=_HEADERS)
    if r.status_code in (401, 403):  # cookies expired / blocked: re-prime once
        await _prime(force=True)
        r = await browser.get("nse", url, params={"symbol": base}, base_headers=_HEADERS)
    r.raise_for_status()
    d = r.json()
    info, meta, sec, price = d.get("info") or {}, d.get("metadata") or {}, d.get("securityInfo") or {}, d.get("priceInfo") or {}
    ind = d.get("industryInfo") or {}
    last = _num(price.get("lastPrice"))
    issued = _num(sec.get("issuedSize"))
    hl = price.get("weekHighLow") or {}
    out = {
        "name": info.get("companyName"), "sector": ind.get("sector") or ind.get("macro"), "industry": ind.get("basicIndustry") or ind.get("industry"),
        "pe": _num(meta.get("pdSymbolPe")), "sector_pe": _num(meta.get("pdSectorPe")),
        "market_cap": round(last * issued) if last and issued else None,
        "fifty_two_week_high": _num(hl.get("max")), "fifty_two_week_low": _num(hl.get("min")),
    }
    return out if any(v is not None for k, v in out.items() if k not in ("name",)) else None
