from __future__ import annotations

from datetime import UTC, datetime

from fm_common.redis import get_redis


async def deny_access_token(jti: str, exp: datetime) -> None:
    ttl = int((exp - datetime.now(UTC)).total_seconds())
    if ttl > 0:
        await get_redis().set(f"fm:jti:deny:{jti}", b"1", ex=ttl)


async def is_access_token_denied(jti: str) -> bool:
    return bool(await get_redis().exists(f"fm:jti:deny:{jti}"))
