"""Real-Time Price Stream Engine (E11).

Poller (one per market-svc replica; a Redis lock makes exactly one of them active):
    hot symbols → provider.quotes() → L1/L2 cache → PUBLISH fm:prices

Hub (every replica): one Redis SUBSCRIBE per process fanned out to all connected browser
WebSockets, each filtered to the symbols that socket subscribed to.

Big moves (±3/5/8/10 % on the day) emit a ``price.moved`` domain event once per symbol,
threshold and day, which wakes the Advisor's Drawdown Sentinel for affected holdings."""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import WebSocket

from fm_common.events import once, publish
from fm_common.logging import get_logger
from fm_common.redis import get_redis

from .config import settings
from .providers import get_provider
from .quotes import encode_ticks, hot_symbols, mark_hot, store_quotes

log = get_logger(__name__)
CHANNEL = "fm:prices"
LOCK_KEY = "fm:mkt:poller-lock"
MOVE_THRESHOLDS = (3.0, 5.0, 8.0, 10.0)
OWNER = f"{socket.gethostname()}-{os.getpid()}".encode()


async def poller() -> None:
    r = get_redis()
    interval = settings.market_poll_interval_seconds
    lock_ttl = int(interval * 3) + 1
    provider = get_provider()
    while True:
        try:
            got = await r.set(LOCK_KEY, OWNER, nx=True, ex=lock_ttl)
            if not got and await r.get(LOCK_KEY) != OWNER:
                await asyncio.sleep(interval)
                continue
            await r.expire(LOCK_KEY, lock_ttl)
            # NAVs (mutual funds / NPS) change once a day after close — never polled intraday
            symbols = [s for s in await hot_symbols() if not (s.isdigit() or s.startswith("NPS-"))]
            if symbols:
                fetched = await provider.quotes(symbols)
                data = {s: q.to_dict() for s, q in fetched.items()}
                await store_quotes(data)
                await r.publish(CHANNEL, encode_ticks(data))
                await _emit_big_moves(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stream.poll_failed", error=str(exc))
        await asyncio.sleep(interval)


async def _emit_big_moves(data: dict[str, dict[str, Any]]) -> None:
    today = datetime.now(UTC).date().isoformat()
    for sym, q in data.items():
        pct = abs(q.get("change_pct") or 0)
        for t in MOVE_THRESHOLDS:
            if pct >= t and await once(f"move:{sym}:{t}:{today}"):
                await publish("price.moved", {"symbol": sym, "change_pct": q["change_pct"], "threshold": t, "price": q["price"]})


class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, set[str]] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen(), name="price-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = get_redis().pubsub()
                await pubsub.subscribe(CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message" or not self.clients:
                        continue
                    ticks = orjson.loads(msg["data"])["data"]
                    await asyncio.gather(*(self._send(ws, subs, ticks) for ws, subs in list(self.clients.items())))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("stream.hub_error", error=str(exc))
                await asyncio.sleep(1)

    async def _send(self, ws: WebSocket, subs: set[str], ticks: list[dict[str, Any]]) -> None:
        mine = [t for t in ticks if t["symbol"] in subs]
        if not mine:
            return
        try:
            await ws.send_bytes(orjson.dumps({"type": "ticks", "data": mine}))
        except Exception:
            self.clients.pop(ws, None)

    async def serve(self, ws: WebSocket) -> None:
        self.clients[ws] = set()
        try:
            while True:
                msg = orjson.loads(await ws.receive_text())
                subs = self.clients[ws]
                if syms := msg.get("subscribe"):
                    syms = [str(s) for s in syms][:200]
                    subs.update(syms)
                    await mark_hot(syms)
                if syms := msg.get("unsubscribe"):
                    subs.difference_update(syms)
                if msg.get("type") == "ping":
                    await mark_hot(list(subs))  # keep them hot while the tab is open
                    await ws.send_text('{"type":"pong"}')
        finally:
            self.clients.pop(ws, None)


hub = Hub()
