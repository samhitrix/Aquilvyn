"""Multi-Level Caching Engine: L1 in-process LRU/TTL  ->  L2 Redis  ->  loader.

* L1 hit: ~microseconds (no I/O). L2 hit: ~1 ms on a local Redis.
* Stampede protection: concurrent misses for the same key share one in-flight loader.
* Cross-instance coherence: ``invalidate()`` deletes from Redis and publishes on
  ``cache:invalidate`` so every API replica drops its L1 copy.
"""
from .engine import CacheEngine, cache

__all__ = ["CacheEngine", "cache"]
