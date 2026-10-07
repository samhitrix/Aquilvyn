from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

_MISSING = object()


class L1Cache:
    """Bounded LRU with per-entry TTL. Single-threaded asyncio use -> no locks needed."""

    def __init__(self, max_items: int = 5000, default_ttl: float = 5.0) -> None:
        self.max_items = max_items
        self.default_ttl = default_ttl
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, default: Any = _MISSING) -> Any:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return default
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            self.misses += 1
            return default
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._data[key] = (time.monotonic() + (ttl if ttl is not None else self.default_ttl), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        for k in [k for k in self._data if k.startswith(prefix)]:
            del self._data[k]

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


MISSING = _MISSING
