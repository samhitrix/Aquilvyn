"""What is this service busy with? A tiny always-on profiler, cheap enough for production.

Each service process has one event loop. When code on it computes for a while without awaiting, every other request
in that process waits — health checks included (they then report "failing postgres / redis" though the databases are
fine). This module records:

* **where the loop was blocked** — a watchdog *thread* notices when the loop's heartbeat is late (> 250 ms) and samples
  the loop thread's stack every 50 ms until it is back; the samples are grouped by the service's own code line;
* **per-route time** — requests, total and slowest time per route (ids collapsed: ``/holdings/{id}``);
* **requests in flight** right now, and the process's CPU use (all threads) over the last minute.

Every process writes its numbers to Redis every 10 s (``fm:stats:<service>:<pid>``), so ``/health/stats`` answers
for all the service's processes, and ``python scripts/fm.py perf`` prints them. Code locations and route shapes
only — no data, ids or names.
"""
from __future__ import annotations

import asyncio
import collections
import os
import re
import sys
import threading
import time
import traceback
from typing import Any

import orjson

LATE_S = 0.25        # the loop is "blocked" when its heartbeat is this late
SAMPLE_S = 0.05      # how often the watchdog samples a blocked loop
BEAT_S = 0.1
PUBLISH_S = 10.0
KEY_TTL = 120
_WORD = re.compile(r"[a-z_-]+|v[0-9]+")  # our route words are lower-case; anything else in a path is an id or a symbol
_OWN = ("_svc/", "_svc\\", "fm_common/", "fm_common\\")


def route_of(path: str) -> str:
    """'/api/v1/market/history/RELIANCE.NS' → '/api/v1/market/history/{id}' (ids, numbers and symbols collapsed)."""
    return "/".join(p if not p or _WORD.fullmatch(p) else "{id}" for p in path.split("/"))


def _where(frame: Any) -> tuple[str, str]:
    """(innermost line of our own code, innermost line of any code) for a stack."""
    stack = traceback.extract_stack(frame)
    own = next((f for f in reversed(stack) if any(p in f.filename for p in _OWN) and "loopwatch" not in f.filename), None)
    leaf = stack[-1] if stack else None

    def fmt(f: Any) -> str:
        if f is None:
            return "?"
        name = f.filename.replace("\\", "/")
        for mark in ("site-packages/", "dist-packages/", "/services/", "/libs/"):
            if mark in name:
                name = name.split(mark, 1)[1]
        return f"{name}:{f.lineno} {f.name}"

    return fmt(own), fmt(leaf)


class LoopWatch:
    def __init__(self) -> None:
        self.service = "?"
        self.started = time.time()
        self.beat = time.perf_counter()
        self.loop_thread: int | None = None
        self.blocked: collections.Counter[str] = collections.Counter()   # own code line → ms blocked
        self.leaf: dict[str, collections.Counter[str]] = {}               # own code line → innermost frames
        self.blocked_ms = 0.0
        self.max_lag_ms = 0.0
        self.routes: dict[str, list[float]] = {}                          # route → [count, total ms, max ms]
        self.inflight: dict[int, tuple[str, float]] = {}
        self.cpu: collections.deque[tuple[float, float]] = collections.deque(maxlen=13)  # (wall, process cpu) every 5 s
        self._stop = threading.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    # ---- requests (called by RequestContextMiddleware) ----
    def request_started(self, key: int, method: str, path: str) -> None:
        self.inflight[key] = (f"{method} {route_of(path)}", time.perf_counter())

    def request_done(self, key: int, ms: float) -> None:
        what = self.inflight.pop(key, None)
        if what is None:
            return
        r = self.routes.setdefault(what[0], [0, 0.0, 0.0])
        r[0] += 1
        r[1] += ms
        r[2] = max(r[2], ms)

    # ---- the watchdog ----
    def _watch(self) -> None:
        frames = sys._current_frames  # noqa: SLF001 — the only way to see another thread's stack
        next_cpu = 0.0
        last = time.perf_counter()
        while not self._stop.wait(SAMPLE_S):
            now = time.perf_counter()
            step, last = now - last, now  # real time since the last look (a busy GIL can make it longer than SAMPLE_S)
            if now >= next_cpu:
                self.cpu.append((now, time.process_time()))
                next_cpu = now + 5
            late = now - self.beat
            if late < LATE_S or self.loop_thread is None:
                continue
            self.max_lag_ms = max(self.max_lag_ms, late * 1000)
            frame = frames().get(self.loop_thread)
            if frame is None:
                continue
            own, leaf = _where(frame)
            ms = min(step, late) * 1000
            self.blocked[own] += ms
            self.leaf.setdefault(own, collections.Counter())[leaf] += 1
            self.blocked_ms += ms

    async def _heartbeat(self) -> None:
        while True:
            self.beat = time.perf_counter()
            await asyncio.sleep(BEAT_S)

    async def _publish(self) -> None:
        from fm_common.redis import get_redis

        while True:
            await asyncio.sleep(PUBLISH_S)
            try:
                await get_redis().set(f"fm:stats:{self.service}:{os.getpid()}", orjson.dumps(self.snapshot()), ex=KEY_TTL)
            except Exception:  # noqa: BLE001 — stats are best effort
                pass

    async def start(self, service: str) -> None:
        self.service = service
        self.loop_thread = threading.get_ident()
        self.beat = time.perf_counter()
        self._tasks = [asyncio.create_task(self._heartbeat(), name="loopwatch-beat"),
                       asyncio.create_task(self._publish(), name="loopwatch-publish")]
        threading.Thread(target=self._watch, name="loopwatch", daemon=True).start()

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()

    # ---- the report ----
    def cpu_pct(self) -> float | None:
        if len(self.cpu) < 2:
            return None
        (w0, c0), (w1, c1) = self.cpu[0], self.cpu[-1]
        return round((c1 - c0) / (w1 - w0) * 100, 1) if w1 > w0 else None

    def snapshot(self) -> dict[str, Any]:
        now = time.perf_counter()
        return {
            "name": self.service, "pid": os.getpid(), "uptime_s": round(time.time() - self.started), "cpu_pct_1m": self.cpu_pct(),
            "cpu_s_total": round(time.process_time(), 1), "threads": threading.active_count(),
            "loop": {"blocked_ms": round(self.blocked_ms), "max_lag_ms": round(self.max_lag_ms),
                     "lag_now_ms": round(max(0.0, now - self.beat) * 1000),
                     "top": [{"where": k, "ms": round(v), "inside": [f for f, _ in self.leaf.get(k, collections.Counter()).most_common(2)]}
                             for k, v in self.blocked.most_common(8)]},
            "routes": sorted(({"route": k, "n": int(v[0]), "total_ms": round(v[1]), "max_ms": round(v[2])} for k, v in self.routes.items()),
                             key=lambda r: -r["total_ms"])[:12],
            "inflight": sorted(({"route": r, "age_ms": round((now - t) * 1000)} for r, t in list(self.inflight.values())),
                               key=lambda r: -r["age_ms"])[:10],
        }


watch = LoopWatch()


async def all_processes(service: str) -> list[dict[str, Any]]:
    """This service's numbers from every process — its API processes and its worker (each writes them every 10 s);
    this process's are always fresh."""
    from fm_common.redis import get_redis

    out = {os.getpid(): watch.snapshot()}
    try:
        r = get_redis()
        keys = [k async for k in r.scan_iter(match=f"fm:stats:{service}*", count=100)]
        for raw in await r.mget(keys) if keys else []:
            if raw:
                d = orjson.loads(raw)
                if d.get("name") in (service, f"{service}-worker"):
                    out.setdefault(d.get("pid"), d)
    except Exception:  # noqa: BLE001
        pass
    return list(out.values())
