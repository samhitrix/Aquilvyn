"""Event Bus Engine (E33a) — Redis Streams with consumer groups.

* ``publish()`` appends a versioned envelope to ``fm:ev:<event_type>`` (capped length).
* ``EventConsumer`` reads with XREADGROUP, calls the handler, XACKs on success, and
  reclaims messages stuck on dead consumers (XAUTOCLAIM) so nothing is lost.
* Handlers must be idempotent; ``once()`` gives a cheap Redis-backed dedupe.

Swapping to NATS/Kafka later means re-implementing this module only.
"""
from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import orjson
from redis.exceptions import ResponseError

from fm_common.config import settings
from fm_common.logging import bind_context, get_logger
from fm_common.redis import get_redis

log = get_logger(__name__)
STREAM_PREFIX = "fm:ev:"
MAXLEN = 100_000

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def envelope(event_type: str, payload: dict[str, Any], household_id: str | None = None, version: int = 1) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "version": version,
        "source": settings.service_name,
        "household_id": household_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }


async def publish(event_type: str, payload: dict[str, Any], household_id: str | None = None) -> str:
    env = envelope(event_type, payload, household_id)
    try:
        await get_redis().xadd(
            STREAM_PREFIX + event_type, {"e": orjson.dumps(env, default=str)}, maxlen=MAXLEN, approximate=True
        )
    except Exception as exc:  # events are best-effort; the nightly run is the safety net
        log.warning("events.publish_failed", event_type=event_type, error=str(exc))
    return env["id"]


async def once(key: str, ttl: int = 86_400) -> bool:
    """True the first time ``key`` is seen (idempotency guard)."""
    return bool(await get_redis().set(f"fm:ev:seen:{key}", b"1", nx=True, ex=ttl))


class EventConsumer:
    def __init__(self, group: str, handlers: dict[str, Handler], block_ms: int = 5000, batch: int = 50) -> None:
        self.group = group
        self.handlers = handlers
        self.block_ms = block_ms
        self.batch = batch
        self.consumer = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self._task: asyncio.Task[None] | None = None

    @property
    def streams(self) -> list[str]:
        return [STREAM_PREFIX + t for t in self.handlers]

    async def _ensure_groups(self) -> None:
        r = get_redis()
        for stream in self.streams:
            try:
                await r.xgroup_create(stream, self.group, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def _handle(self, stream: str, msg_id: bytes, fields: dict[bytes, bytes]) -> None:
        env = orjson.loads(fields[b"e"])
        handler = self.handlers.get(env["type"])
        if handler is None:
            return
        bind_context(event_id=env["id"], event_type=env["type"])
        await handler(env)

    async def run(self) -> None:
        await self._ensure_groups()
        r = get_redis()
        log.info("events.consumer_started", group=self.group, streams=self.streams)
        while True:
            try:
                # Reclaim anything idle > 60s from crashed consumers first.
                for stream in self.streams:
                    _, claimed, _ = await r.xautoclaim(stream, self.group, self.consumer, min_idle_time=60_000, count=self.batch)
                    for msg_id, fields in claimed:
                        await self._process(stream, msg_id, fields)
                resp = await r.xreadgroup(
                    self.group, self.consumer, {s: ">" for s in self.streams}, count=self.batch, block=self.block_ms
                )
                for stream, messages in resp or []:
                    stream = stream.decode() if isinstance(stream, bytes) else stream
                    for msg_id, fields in messages:
                        await self._process(stream, msg_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("events.consumer_error", error=str(exc))
                await asyncio.sleep(2)

    async def _process(self, stream: str, msg_id: bytes, fields: dict[bytes, bytes]) -> None:
        try:
            await self._handle(stream, msg_id, fields)
            await get_redis().xack(stream, self.group, msg_id)
        except Exception as exc:  # left pending -> retried via XAUTOCLAIM
            log.error("events.handler_failed", stream=stream, error=str(exc))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"events-{self.group}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
