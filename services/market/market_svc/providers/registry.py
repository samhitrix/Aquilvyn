"""Composite provider: routes each symbol to the right source.

* AMFI scheme codes (all digits) → mfapi (or simulator)
* NPS-*  → simulator in P1 (manual NAV entry also supported; P2: PFRDA/NSDL feed)
* everything else (NSE/BSE/indices) → Yahoo (or simulator)
Failures fall back to the simulator only when ``market_data_provider=simulated``; in live mode
a failed quote is reported as missing so the Data Quality Engine can flag it — never faked."""
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from ..config import settings
from .base import Bar, Quote
from .simulated import SimulatedProvider


def is_mf_code(symbol: str) -> bool:
    return symbol.isdigit()


class RealNAVProvider:
    """Mutual-fund NAVs always come from AMFI (via mfapi.in: free, no key), even in simulated
    mode — a made-up NAV on a real holding is worse than no NAV. NAVs are published once a day
    after market close, so they never tick intraday. Offline + simulated mode → simulator."""

    name = "mfapi"

    def __init__(self, sim: SimulatedProvider, allow_sim_fallback: bool) -> None:
        from .mfapi import MFAPIProvider

        self.api = MFAPIProvider()
        self.sim = sim
        self.fallback = allow_sim_fallback

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = await self.api.quotes(symbols)
        missing = [s for s in symbols if s not in out]
        if missing and self.fallback:
            out.update(await self.sim.quotes(missing))
        return out

    async def _try(self, method: str, *args: Any) -> Any:
        try:
            return await getattr(self.api, method)(*args)
        except Exception:
            if not self.fallback:
                raise
            return await getattr(self.sim, method)(*args)

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        return await self._try("history", symbol, days)

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        return await self._try("fundamentals", symbol)

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self.api.search(query, limit)

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        return []


class NoSource:
    """A source that has nothing — callers fall back to statement / last-known values."""

    name = "none"

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {}

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        return []

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        return None

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        return []


class CompositeProvider:
    def __init__(self, live: bool) -> None:
        self.live = live
        self.none = NoSource()
        self.sim = SimulatedProvider()
        self.mf: Any = RealNAVProvider(self.sim, allow_sim_fallback=not live)
        if live:
            from .yahoo import YahooProvider

            self.equity: Any = YahooProvider()
        else:
            self.equity = self.sim
        self.name = "yahoo+mfapi" if live else "simulated+mfapi"

    def _route(self, symbol: str) -> Any:
        if symbol.startswith("NPS-"):
            # no free live NPS NAV feed yet: statement-imported schemes (…-T1/-T2) and live mode use the
            # NAV from the imported statement; only the demo universe's schemes are simulated
            return self.sim if not self.live and not symbol.endswith(("-T1", "-T2")) else self.none
        return self.mf if is_mf_code(symbol) else self.equity

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        groups: dict[int, tuple[Any, list[str]]] = {}
        for s in symbols:
            p = self._route(s)
            groups.setdefault(id(p), (p, []))[1].append(s)
        out: dict[str, Quote] = {}
        for part in await asyncio.gather(*(p.quotes(syms) for p, syms in groups.values())):
            out.update(part)
        return out

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        return await self._route(symbol).history(symbol, days)

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        return await self._route(symbol).fundamentals(symbol)

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self._route(symbol).news(symbol, limit)

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        results = await self.equity.search(query, limit)
        try:
            results += await self.mf.search(query, limit)
        except Exception:
            pass
        return results[: limit * 2]

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        return await self._route(symbol).corporate_actions(symbol)


@lru_cache
def get_provider() -> CompositeProvider:
    return CompositeProvider(live=settings.market_data_provider == "yahoo")
