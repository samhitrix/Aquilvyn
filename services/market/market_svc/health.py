"""Data-source health (E13 Data Quality): each provider records its last success / failure so
the app can say *why* prices are missing ("Yahoo: rate-limited 2 min ago") instead of silently
showing values at cost."""
from __future__ import annotations

import time
from typing import Any

import orjson

from fm_common.redis import get_redis

KEY = "fm:mkt:health"


async def record(source: str, ok: bool | None, error: str | None = None, count: int = 0, note: str | None = None) -> None:
    """``note``: informational (e.g. symbols a source doesn't list) — never marks the source failing.
    ``ok=None`` updates only the note ("" clears it)."""
    r = get_redis()
    raw = await r.hget(KEY, source)
    cur: dict[str, Any] = orjson.loads(raw) if raw else {}
    now = time.time()
    if note is not None:
        cur["note"] = note[:300] or None
    if ok is None:
        pass
    elif ok:
        cur.update(last_ok=now, last_count=count)
    else:
        cur.update(last_error_at=now, last_error=(error or "failed")[:300])
    await r.hset(KEY, source, orjson.dumps(cur))


async def snapshot() -> dict[str, dict[str, Any]]:
    raw = await get_redis().hgetall(KEY)
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        d = orjson.loads(v)
        ok_at, err_at = d.get("last_ok"), d.get("last_error_at")
        d["status"] = "ok" if ok_at and (not err_at or ok_at >= err_at) else "failing" if err_at else "unknown"
        d["last_ok_ago_s"] = int(now - ok_at) if ok_at else None
        d["last_error_ago_s"] = int(now - err_at) if err_at else None
        out[k.decode() if isinstance(k, bytes) else k] = d
    return out
