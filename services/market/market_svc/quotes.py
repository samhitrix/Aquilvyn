"""Quotes through the Multi-Level Cache (L1 1s / L2 = poll interval) + the live "hot set".

Any symbol someone asks about joins ``fm:mkt:hot`` (a sorted set scored by last-interest time);
the Price Stream Engine polls only hot symbols, so provider load scales with real interest."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import orjson

from fm_common.cache import cache
from fm_common.cache.l1 import MISSING
from fm_common.logging import get_logger
from fm_common.redis import get_redis

from . import health
from .config import settings
from .providers import get_provider
from .providers.base import Quote

log = get_logger(__name__)

HOT_KEY = "fm:mkt:hot"
QUOTE_KEY = "quote:"
MISS_KEY = "quote-miss:"  # symbols the provider couldn't price; not retried on every request
MISS_TTL_SECONDS = 300
NAV_TTL_SECONDS = 3600


async def mark_hot(symbols: list[str]) -> None:
    if symbols:
        await get_redis().zadd(HOT_KEY, {s: time.time() for s in symbols})


async def hot_symbols() -> list[str]:
    r = get_redis()
    cutoff = time.time() - settings.market_hot_symbols_ttl_seconds
    await r.zremrangebyscore(HOT_KEY, 0, cutoff)
    return [s.decode() for s in await r.zrange(HOT_KEY, 0, -1)]


LAST_KEY = "quote-last:"   # last good quote per symbol (7 days) — shown, marked stale, when a source is down
LAST_TTL_SECONDS = 7 * 86400
FETCH_WAIT_SECONDS = 4.0     # callers get whatever is ready by then; the fetch finishes in the background
RECENT_SECONDS = 1800        # a last good quote younger than this is served at once (refreshed in the background)
_inflight: dict[str, asyncio.Task[Any]] = {}


def _ttl(sym: str) -> int:
    nav = sym.isdigit() or sym.startswith("NPS-")  # daily NAV: re-checked hourly, not every few seconds
    return NAV_TTL_SECONDS if nav else max(settings.quote_cache_ttl_seconds, int(settings.market_poll_interval_seconds * 2))


async def _fetch_and_store(missing: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        fetched = await get_provider().quotes(missing)
    except Exception as exc:
        log.warning("quotes.fetch_failed", n=len(missing), error=str(exc))
        await health.record("quotes", ok=False, error=str(exc) or type(exc).__name__)
        fetched = {}
    for sym, q in fetched.items():
        d = q.to_dict()
        await cache.set(QUOTE_KEY + sym, d, ttl=_ttl(sym), l1_ttl=1)
        await cache.set(LAST_KEY + sym, d, ttl=LAST_TTL_SECONDS, l1_ttl=5)
        out[sym] = d
    for sym in set(missing) - set(fetched):
        await cache.set(MISS_KEY + sym, 1, ttl=MISS_TTL_SECONDS, l1_ttl=30)
    return out


async def get_quotes(symbols: list[str], wait: float = FETCH_WAIT_SECONDS) -> dict[str, dict[str, Any]]:
    """Fresh quotes from cache; missing ones are fetched (deduplicated across requests). Anything
    not ready within ``wait`` seconds falls back to the last good quote (``stale: true``) and is
    filled in the background — a slow or rate-limited source never blocks a page."""
    symbols = list(dict.fromkeys(s for s in symbols if s))
    await mark_hot(symbols)
    live = getattr(get_provider(), "live", False)
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for s in symbols:
        v = await cache.get(QUOTE_KEY + s)
        if isinstance(v, dict) and not (live and str(v.get("source", "")).startswith("simulated")):
            out[s] = v
        elif await cache.get(MISS_KEY + s) is MISSING:  # cache.get returns the MISSING sentinel (truthy!), not None
            missing.append(s)
    todo = [s for s in missing if s not in _inflight]
    if todo:
        task = asyncio.create_task(_fetch_and_store(todo))
        for s in todo:
            _inflight[s] = task
        task.add_done_callback(lambda _t, syms=tuple(todo): [_inflight.pop(x, None) for x in syms])
    # stale-while-revalidate: a recent last-good quote is answered now; only never-priced symbols wait for the source
    waiting = []
    for s in missing:
        last = await cache.get(LAST_KEY + s)
        if isinstance(last, dict) and not (live and str(last.get("source", "")).startswith("simulated")) and _age(last) <= RECENT_SECONDS:
            out[s] = last
        else:
            waiting.append(s)
    missing = waiting
    tasks = {_inflight[s] for s in missing if s in _inflight}
    if tasks:
        done, _ = await asyncio.wait(tasks, timeout=wait)
        for t in done:
            if t.cancelled():
                continue
            if (exc := t.exception()) is not None:
                log.warning("quotes.task_failed", error=repr(exc))
                continue
            out.update({k: v for k, v in t.result().items() if k in missing})
    for s in symbols:
        if s not in out:
            last = await cache.get(LAST_KEY + s)
            if isinstance(last, dict) and not (live and str(last.get("source", "")).startswith("simulated")):
                out[s] = {**last, "stale": True}
    return out


def _age(q: dict[str, Any]) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(str(q.get("ts")))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def statement_only(sym: str) -> bool:
    return sym.startswith("NPS-") and sym.endswith(("-T1", "-T2"))


async def store_reference_quotes(prices: dict[str, dict[str, Any]]) -> int:
    """Prices printed on a statement (e.g. Zerodha holdings 'previous close') — used only when no
    live quote was ever fetched, so values aren't shown 'at cost' when a source is blocked."""
    n = 0
    for sym, p in prices.items():
        cur = await cache.get(LAST_KEY + sym)
        # a real quote always wins; a newer statement replaces an older statement's price. Statement-only
        # instruments (NPS schemes from a CRA statement — no feed) always take the statement's NAV.
        only = statement_only(sym)
        if isinstance(cur, dict) and not only and not (cur.get("source") == "statement" and str(p.get("as_of") or "") >= str(cur.get("ts") or "")[:10]):
            continue
        if only:
            await cache.invalidate(QUOTE_KEY + sym)
        price = float(p["price"])
        d = Quote(sym, price, float(p.get("prev_close") or price), source="statement").to_dict()
        if p.get("as_of"):
            d["ts"] = str(p["as_of"])
        await cache.set(LAST_KEY + sym, d, ttl=LAST_TTL_SECONDS, l1_ttl=5)
        n += 1
    return n


async def store_quotes(quotes: dict[str, dict[str, Any]]) -> None:
    for sym, d in quotes.items():
        await cache.set(QUOTE_KEY + sym, d, ttl=_ttl(sym), l1_ttl=1)
        await cache.set(LAST_KEY + sym, d, ttl=LAST_TTL_SECONDS, l1_ttl=5)


def encode_ticks(quotes: dict[str, dict[str, Any]]) -> bytes:
    return orjson.dumps({"type": "ticks", "data": list(quotes.values())})
