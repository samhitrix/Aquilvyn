"""ISIN → NSE / BSE trading symbol.

Brokers name the same share differently — ICICI Direct uses its own codes ("RELIND"), Kotak / SBI print the
company name, Upstox a scrip code — but every one of them prints the ISIN, which identifies the security
exactly. Resolution order (each result cached for a day):

1. instruments FolioSense already knows with that ISIN;
2. NSE's complete equity list (``EQUITY_L.csv``: SYMBOL, NAME OF COMPANY, SERIES, …, ISIN NUMBER) — every
   NSE-listed share and ETF;
3. Yahoo's search, which also answers ISIN queries (covers BSE-only shares).
"""
from __future__ import annotations

import csv
import io
import re
import time

import orjson

from fm_common.logging import get_logger
from fm_common.redis import get_redis

from . import browser

log = get_logger(__name__)
ISIN_RE = re.compile(r"^IN[EF9][0-9A-Z]{8}[0-9]$")
EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
ETF_LIST = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"
REDIS_KEY = "fm:isin:nse_list"
CACHE_KEY = "fm:isin:{}"
TTL = 24 * 3600
_mem: tuple[float, dict[str, dict[str, str]]] | None = None


def parse_equity_list(text: str) -> dict[str, dict[str, str]]:
    """NSE's list CSVs → {ISIN: {symbol, name}} (headers vary in case / spacing between the two lists)."""
    out: dict[str, dict[str, str]] = {}
    rows = csv.DictReader(io.StringIO(text))
    for r in rows:
        row = {re.sub(r"\s+", " ", (k or "").strip().lower()): (v or "").strip() for k, v in r.items()}
        isin = row.get("isin number") or row.get("isin") or row.get("isin code")
        sym = row.get("symbol")
        if isin and sym and ISIN_RE.match(isin):
            out[isin] = {"symbol": sym, "name": row.get("name of company") or row.get("security name") or row.get("underlying") or sym}
    return out


async def _nse_lists() -> dict[str, dict[str, str]]:
    global _mem
    if _mem and time.monotonic() - _mem[0] < TTL:
        return _mem[1]
    r = get_redis()
    raw = await r.get(REDIS_KEY)
    if raw:
        data = orjson.loads(raw)
    else:
        data = {}
        for url in (EQUITY_LIST, ETF_LIST):
            try:
                resp = await browser.get("nse-archives", url, headers={"Accept": "text/csv,*/*"})
                resp.raise_for_status()
                data.update(parse_equity_list(resp.text))
            except Exception as exc:
                log.warning("isin.nse_list_failed", url=url, error=str(exc)[:160])
        if data:
            await r.set(REDIS_KEY, orjson.dumps(data), ex=TTL)
    _mem = (time.monotonic(), data)
    return data


async def _yahoo(isin: str) -> dict[str, str] | None:
    try:
        resp = await browser.get("yahoo", "https://query2.finance.yahoo.com/v1/finance/search", params={"q": isin, "quotesCount": 5, "newsCount": 0})
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or []
    except Exception as exc:
        log.warning("isin.yahoo_failed", isin=isin, error=str(exc)[:160])
        return None
    for q in quotes:  # prefer NSE, then BSE
        for suffix, exch in ((".NS", "NSE"), (".BO", "BSE")):
            sym = str(q.get("symbol") or "")
            if sym.endswith(suffix):
                return {"symbol": sym.removesuffix(suffix), "exchange": exch, "name": q.get("longname") or q.get("shortname") or sym}
    return None


async def lookup(isins: list[str], known: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """{ISIN: {symbol, exchange, name, via}} for every ISIN that could be resolved."""
    want = sorted({i.strip().upper() for i in isins if i and ISIN_RE.match(i.strip().upper())})
    out: dict[str, dict[str, str]] = {}
    r = get_redis()
    for isin in want:
        if known and isin in known:
            out[isin] = {**known[isin], "via": "known"}
            continue
        cached = await r.get(CACHE_KEY.format(isin))
        if cached:
            out[isin] = orjson.loads(cached)
    missing = [i for i in want if i not in out]
    if missing:
        nse = await _nse_lists()
        for isin in missing:
            hit = {**nse[isin], "exchange": "NSE", "via": "nse"} if isin in nse else None
            if hit is None and not isin.startswith("INF"):  # mutual funds come from the CAS
                y = await _yahoo(isin)
                hit = {**y, "via": "yahoo"} if y else None
            if hit:
                out[isin] = hit
                await r.set(CACHE_KEY.format(isin), orjson.dumps(hit), ex=TTL * 7)
    return out


def nse_list_size() -> int:
    return len(_mem[1]) if _mem else 0


__all__ = ["ISIN_RE", "lookup", "nse_list_size", "parse_equity_list"]
