"""Company size (large / mid / small) from NSE's official index constituent lists.

SEBI defines large cap as the top 100 listed companies by market cap, mid cap as 101–250 and small
cap as the rest. NSE's Nifty 100 and Nifty Midcap 150 indices track exactly those two bands, and
their constituent lists are published as small CSVs — a far more reliable source than per-stock
market-cap lookups. Any NSE stock in neither list is small cap.

CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
"""
from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import Any

import httpx
import orjson

from fm_common.logging import get_logger
from fm_common.redis import get_redis

from .. import health

log = get_logger(__name__)
LISTS = {
    "large": ("https://archives.nseindia.com/content/indices/ind_nifty100list.csv", "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"),
    "mid": ("https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv", "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv"),
}
REDIS_KEY = "fm:nse:size_lists"
TTL = 24 * 3600
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
RETRY_AFTER = 600  # after a failed download, don't retry (and make pages wait) for 10 minutes
_mem: tuple[float, dict[str, Any]] | None = None
_failed_at = -1e9
_lock = asyncio.Lock()


def parse_constituents(text: str) -> dict[str, dict[str, str]]:
    """→ {"symbols": {SYMBOL: industry}, "isins": {ISIN: SYMBOL}}"""
    symbols: dict[str, str] = {}
    isins: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        sym = r.get("symbol", "").upper()
        if not sym:
            continue
        symbols[sym] = r.get("industry", "")
        if r.get("isin code"):
            isins[r["isin code"].upper()] = sym
    return {"symbols": symbols, "isins": isins}


async def size_lists() -> dict[str, Any]:
    """{"large": {...}, "mid": {...}} or {} when NSE can't be reached (callers fall back)."""
    global _mem, _failed_at
    if _mem and time.monotonic() - _mem[0] < TTL:
        return _mem[1]
    if time.monotonic() - _failed_at < RETRY_AFTER:
        return {}
    async with _lock:
        if _mem and time.monotonic() - _mem[0] < TTL:
            return _mem[1]
        if time.monotonic() - _failed_at < RETRY_AFTER:
            return {}
        r = get_redis()
        raw = await r.get(REDIS_KEY)
        data: dict[str, Any] = orjson.loads(raw) if raw else {}
        if not data:
            errors = []
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": UA, "Accept": "text/csv,*/*"}) as c:
                for band, urls in LISTS.items():
                    for url in urls:
                        try:
                            resp = await c.get(url)
                            resp.raise_for_status()
                            parsed = parse_constituents(resp.text)
                            if len(parsed["symbols"]) >= 50:
                                data[band] = parsed
                                break
                        except Exception as exc:
                            errors.append(f"{url}: {exc}")
            if len(data) == 2:
                await r.set(REDIS_KEY, orjson.dumps(data), ex=TTL)
                await health.record("nse_size_lists", ok=True, count=sum(len(v["symbols"]) for v in data.values()))
            else:
                data = {}
                _failed_at = time.monotonic()
                await health.record("nse_size_lists", ok=False, error=(errors[-1] if errors else "incomplete lists")[:300])
                log.warning("nse.size_lists_unavailable", errors=errors[-2:])
        _mem = (time.monotonic(), data) if data else None
        return data


def bucket(lists: dict[str, Any], symbol: str, isin: str | None) -> str | None:
    """large / mid / small for an NSE stock, or None if the lists are unavailable."""
    if not lists:
        return None
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    for band in ("large", "mid"):
        band_data = lists.get(band) or {}
        if base in band_data.get("symbols", {}) or (isin and isin.upper() in band_data.get("isins", {})):
            return band
    return "small"


def industry(lists: dict[str, Any], symbol: str, isin: str | None) -> str | None:
    """NSE's industry for Nifty 100 / Midcap 150 members (a sector fallback when none is known)."""
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    for band in ("large", "mid"):
        d = lists.get(band) or {}
        sym = base if base in d.get("symbols", {}) else d.get("isins", {}).get((isin or "").upper())
        if sym and d["symbols"].get(sym):
            return d["symbols"][sym]
    return None
