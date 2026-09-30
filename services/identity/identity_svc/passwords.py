from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
# Verified against when the user does not exist, so timing doesn't leak account existence.
_DUMMY_HASH = _hasher.hash("timing-equaliser")


async def hash_password(password: str) -> str:
    # Argon2 is deliberately CPU-heavy: keep it off the event loop.
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerifyMismatchError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
