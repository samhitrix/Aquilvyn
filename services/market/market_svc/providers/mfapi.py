"""AMFI mutual-fund NAVs via mfapi.in (free, daily NAV history by AMFI scheme code)."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson

from fm_common.cache import cache
from fm_common.logging import get_logger

from .. import health
from ..config import settings
from .base import Bar, Quote

log = get_logger(__name__)


def plan_from_name(name: str | None) -> str | None:
    """'direct' / 'regular' only when the scheme name says so — never guessed. A name without
    either word (e.g. 'Motilal Oswal Midcap Fund') is unknown, and no switch advice is given."""
    low = (name or "").lower()
    if "direct" in low:
        return "direct"
    if "regular" in low:
        return "regular"
    return None


def _category(raw: str | None) -> str:
    s = (raw or "").lower()
    kind = "debt" if "debt" in s or "bond" in s or "liquid" in s or "gilt" in s else "hybrid" if "hybrid" in s else "equity"
    for key in ("elss", "flexi", "large & mid", "large cap", "mid cap", "small cap", "index", "multi cap", "focused", "value", "liquid", "corporate bond", "gilt"):
        if key in s:
            return f"{kind}:{key.replace(' & ', '_').replace(' ', '_')}"
    return f"{kind}:other"


class MFAPIProvider:
    name = "mfapi"

    # NAVs are published once a day, so a scheme is fetched at most every few hours and shared by every process
    # (Redis) — not re-downloaded on each dashboard load or after a restart. mfapi.in returns a fund's *whole* history
    # (thousands of days, hundreds of KB); only the latest MAX_ROWS days are kept, and the JSON is decoded off the
    # event loop, so a page waiting on prices is never stuck behind it.
    SCHEME_TTL_SECONDS = 3 * 3600
    MAX_ROWS = 2000  # ~8 years of NAVs: more than any chart or check uses

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=10, base_url=settings.mf_nav_api)
        self._schemes: dict[str, tuple[float, dict[str, Any]]] = {}
        self._sem = asyncio.Semaphore(8)

    async def _download(self, code: str) -> dict[str, Any]:
        async with self._sem:
            r = await self._client.get(f"/{code}")
        r.raise_for_status()
        data = await asyncio.to_thread(orjson.loads, r.content)
        if isinstance(data.get("data"), list):
            data["data"] = data["data"][: self.MAX_ROWS]  # newest first
        return data

    async def _scheme(self, code: str) -> dict[str, Any]:
        hit = self._schemes.get(code)
        if hit and time.monotonic() - hit[0] < self.SCHEME_TTL_SECONDS:
            return hit[1]
        data = await cache.get_or_load(f"mfapi:{code}", lambda: self._download(code), ttl=self.SCHEME_TTL_SECONDS, l1_ttl=1)
        if data.get("data"):
            if len(self._schemes) > 2000:
                self._schemes.clear()
            self._schemes[code] = (time.monotonic(), data)
        return data

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        async def one(code: str) -> Quote | None:
            try:
                data = (await self._scheme(code))["data"]
                nav, prev = float(data[0]["nav"]), float(data[1]["nav"]) if len(data) > 1 else float(data[0]["nav"])
                ts = datetime.strptime(data[0]["date"], "%d-%m-%Y").replace(tzinfo=UTC)
                return Quote(code, nav, prev, source=self.name, ts=ts)
            except Exception as exc:
                log.warning("mfapi.quote_failed", code=code, error=str(exc))
                return None

        results = await asyncio.gather(*(one(c) for c in symbols))  # concurrent; bounded by the semaphore
        out = {q.symbol: q for q in results if q is not None}
        if out:
            await health.record("amfi_nav", ok=True, count=len(out))
        if len(out) < len(symbols):
            await health.record("amfi_nav", ok=False, error=f"{len(symbols) - len(out)}/{len(symbols)} NAVs unavailable from mfapi.in")
        return out

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        data = (await self._scheme(symbol))["data"]
        bars = []
        for row in reversed(data):
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
            v = float(row["nav"])
            bars.append(Bar(d, v, v, v, v, 0))
        return bars[-days:] if days < len(bars) else bars

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        meta = (await self._scheme(symbol)).get("meta", {})
        return {
            "name": meta.get("scheme_name"), "amc": meta.get("fund_house"),
            "mf_category": _category(meta.get("scheme_category")),
            "plan": plan_from_name(meta.get("scheme_name")),
            "isin": meta.get("isin_growth"),
        }

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        r = await self._client.get("/search", params={"q": query})
        r.raise_for_status()
        return [{"symbol": str(x["schemeCode"]), "name": x["schemeName"], "asset_type": "mutual_fund"} for x in r.json()[:limit]]

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        return []
