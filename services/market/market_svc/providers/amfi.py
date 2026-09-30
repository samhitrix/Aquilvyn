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
REDIS_KEY = "fm:amfi:master"
TTL = 24 * 3600
_mem: tuple[float, dict[str, dict[str, Any]]] | None = None
_lock = asyncio.Lock()


def parse_navall(text: str) -> dict[str, dict[str, Any]]:
    """ISIN → {code, name, nav, date}. Both ISIN columns (growth / reinvest) map to the same code."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        code, isin1, isin2, name, nav, dt = parts[:6]
        try:
            navf = float(nav)
        except ValueError:
            navf = None
        row = {"code": code, "name": name, "nav": navf, "date": dt}
        for isin in (isin1, isin2):
            if re.fullmatch(r"INF[0-9A-Z]{9}", isin or ""):
                out[isin] = row
    return out


async def master() -> dict[str, dict[str, Any]]:
    global _mem
    if _mem and time.monotonic() - _mem[0] < TTL:
        return _mem[1]
    async with _lock:
        if _mem and time.monotonic() - _mem[0] < TTL:
            return _mem[1]
        r = get_redis()
        raw = await r.get(REDIS_KEY)
        if raw:
            data = orjson.loads(raw)
        else:
            data = {}
            last_err = ""
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 FolioSense"}) as c:
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
        _mem = (time.monotonic(), data) if data else None
        return data


async def by_isin(isins: list[str]) -> dict[str, dict[str, Any]]:
    m = await master()
    return {i: m[i] for i in isins if i in m}
