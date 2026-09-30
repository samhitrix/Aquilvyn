"""Screener.in company page — a free, key-less fundamentals source for NSE/BSE stocks.

The public page ``/company/<SYMBOL>/consolidated/`` lists the headline ratios (market cap, P/E, book value,
dividend yield, ROCE, ROE, 52-week high/low), the compounded sales / profit growth tables and the
shareholding pattern (promoter %). Parsed with plain regexes on the stable ids/classes; anything not found is
left as None so later sources can fill it.
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Any

from . import browser

_sem = asyncio.Semaphore(2)  # be polite: a handful of pages per analysis run, never a burst
BASE = "https://www.screener.in/company/{sym}/{view}"


def _n(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _top_ratios(page: str) -> dict[str, list[float | None]]:
    m = re.search(r'<ul[^>]*id="top-ratios"[^>]*>(.*?)</ul>', page, re.S)
    out: dict[str, list[float | None]] = {}
    if not m:
        return out
    for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S):
        name = re.search(r'<span[^>]*class="[^"]*\bname\b[^"]*"[^>]*>(.*?)</span>', li, re.S)
        if not name:
            continue
        out[_text(name.group(1)).lower()] = [_n(x) for x in re.findall(r'<span[^>]*class="[^"]*\bnumber\b[^"]*"[^>]*>([^<]*)</span>', li)]
    return out


def _ranges(page: str, title: str) -> dict[str, float | None]:
    """'Compounded Sales Growth' → {'10 years': 7.0, '5 years': 12.0, '3 years': 21.0, 'ttm': 5.0}."""
    for tbl in re.findall(r'<table[^>]*class="[^"]*ranges-table[^"]*"[^>]*>(.*?)</table>', page, re.S):
        if title.lower() not in _text(tbl).lower():
            continue
        rows = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", tbl, re.S)
        return {_text(k).rstrip(":").lower(): _n(_text(v)) for k, v in rows}
    return {}


def _promoters(page: str) -> float | None:
    sec = re.search(r'<section[^>]*id="shareholding"[^>]*>(.*?)</section>', page, re.S)
    if not sec:
        return None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", sec.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if cells and _text(cells[0]).lower().startswith("promoters"):
            vals = [_n(_text(c)) for c in cells[1:]]
            vals = [v for v in vals if v is not None]
            return vals[-1] if vals else None  # latest quarter is the last column
    return None


def parse(page: str) -> dict[str, Any] | None:
    top = _top_ratios(page)
    if not top:
        return None

    def first(*names: str, idx: int = 0) -> float | None:
        for n in names:
            vals = top.get(n) or []
            if len(vals) > idx and vals[idx] is not None:
                return vals[idx]
        return None
    price, book = first("current price"), first("book value")
    cap_cr = first("market cap")
    div = first("dividend yield")
    sales, profit = _ranges(page, "Compounded Sales Growth"), _ranges(page, "Compounded Profit Growth")
    name = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    out = {
        "name": _text(name.group(1)) if name else None,
        "market_cap": round(cap_cr * 1e7) if cap_cr is not None else None,  # ₹ crore → ₹
        "pe": first("stock p/e"), "book_value": book,
        "pb": round(price / book, 2) if price and book else None,
        "dividend_yield": round(div / 100, 4) if div is not None else None,  # stored as a fraction, like Yahoo's
        "roce": first("roce"), "roe": first("roe"),
        "fifty_two_week_high": first("high / low", idx=0), "fifty_two_week_low": first("high / low", idx=1),
        "revenue_growth": sales.get("ttm"), "earnings_growth": profit.get("ttm"),
        "revenue_cagr_3y": sales.get("3 years"), "eps_cagr_3y": profit.get("3 years"),
        "promoter_holding": _promoters(page),
    }
    return out if any(v is not None for k, v in out.items() if k != "name") else None


async def fundamentals(symbol: str) -> dict[str, Any] | None:
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    if not base or base.startswith("^"):
        return None
    async with _sem:
        for view in ("consolidated/", ""):  # consolidated first; some companies only have standalone numbers
            r = await browser.get("screener", BASE.format(sym=base, view=view))
            if r.status_code == 404:
                return None  # not a listed company on Screener (e.g. an ETF)
            r.raise_for_status()
            data = parse(r.text)
            if data and (data.get("pe") is not None or data.get("roe") is not None):
                return data
        return data
