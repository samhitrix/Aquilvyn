"""AMFI scheme master (NAVAll.txt): the official daily file listing every open mutual-fund
scheme with its AMFI code, ISINs, name and latest NAV. Used to map an ISIN from a holdings
statement (Zerodha Coin, CAMS, etc.) to the AMFI code that NAV sources are keyed by.

Format (semicolon-separated, with AMC / category header lines in between):
    Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx
import orjson

from fm_common.logging import get_logger
from fm_common.redis import get_redis

from .. import health

log = get_logger(__name__)
URLS = ("https://portal.amfiindia.com/spages/NAVAll.txt", "https://www.amfiindia.com/spages/NAVAll.txt")
REDIS_KEY = "fm:amfi:master:v3"  # v2: rows carry the fund house (amc); v3: and the category
TTL = 24 * 3600
_mem: tuple[float, dict[str, dict[str, Any]]] | None = None
FAIL_TTL = 300  # after a failed download, don't retry on every request
_by_code: tuple[int, dict[str, dict[str, Any]]] | None = None
_lock = asyncio.Lock()


def parse_navall(text: str) -> dict[str, dict[str, Any]]:
    """ISIN → {code, name, nav, date, amc, category}. Both ISIN columns (growth / reinvest) map to the same code.
    The fund house (AMC) is the header line above each block ("HDFC Mutual Fund"); the category is the line
    above that ("Open Ended Schemes(Debt Scheme - Liquid Fund)")."""
    out: dict[str, dict[str, Any]] = {}
    amc = category = ""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) == 1 and parts[0].lower().endswith("mutual fund"):
            amc = parts[0]
            continue
        if len(parts) == 1 and re.match(r"(?i)(open|close|interval)\w*\s+ended|interval", parts[0]):
            category = parts[0]
            continue
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        code, isin1, isin2, name, nav, dt = parts[:6]
        try:
            navf = float(nav)
        except ValueError:
            navf = None
        row = {"code": code, "name": name, "nav": navf, "date": dt, "amc": amc, "category": category}
        for isin in (isin1, isin2):
            if re.fullmatch(r"INF[0-9A-Z]{9}", isin or ""):
                out[isin] = row
    return out


async def master() -> dict[str, dict[str, Any]]:
    global _mem
    if _mem and time.monotonic() - _mem[0] < (TTL if _mem[1] else FAIL_TTL):
        return _mem[1]
    async with _lock:
        if _mem and time.monotonic() - _mem[0] < (TTL if _mem[1] else FAIL_TTL):
            return _mem[1]
        r = get_redis()
        raw = await r.get(REDIS_KEY)
        if raw:
            data = orjson.loads(raw)
        else:
            data = {}
            last_err = ""
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Aquilvyn"}) as c:
                for url in URLS:
                    try:
                        resp = await c.get(url)
                        resp.raise_for_status()
                        data = parse_navall(resp.text)
                        if data:
                            break
                    except Exception as exc:
                        last_err = f"{url}: {exc}"
            if data:
                await r.set(REDIS_KEY, orjson.dumps(data), ex=TTL)
                await health.record("amfi_master", ok=True, count=len(data))
            else:
                await health.record("amfi_master", ok=False, error=last_err or "empty scheme master")
                log.warning("amfi.master_unavailable", error=last_err)
        _mem = (time.monotonic(), data)
        return data


async def by_isin(isins: list[str]) -> dict[str, dict[str, Any]]:
    m = await master()
    return {i: m[i] for i in isins if i in m}


async def by_code() -> dict[str, dict[str, Any]]:
    """AMFI scheme code → {code, name, amc, category, isins} (built once per scheme-list download)."""
    global _by_code
    m = await master()
    if _by_code and _by_code[0] == id(m):
        return _by_code[1]
    out: dict[str, dict[str, Any]] = {}
    for isin, row in m.items():
        e = out.setdefault(row["code"], {"code": row["code"], "name": row["name"], "amc": row.get("amc", ""),
                                         "category": row.get("category", ""), "isins": []})
        e["isins"].append(isin)
    if m:
        _by_code = (id(m), out)
    return out
