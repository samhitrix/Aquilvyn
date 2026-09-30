"""Refresh Token Rotation.

* Every refresh consumes the presented token and issues a new one in the same family.
* Lifetime of each token: ``refresh_token_ttl_days`` (7) — sliding with use, so an active
  user stays signed in and an idle one is logged out after a week.
* Reuse detection: presenting an already-rotated token revokes the whole family (stolen-token
  scenario, RFC 6819 §5.2.2.3). A 15 s grace window absorbs benign races (two browser tabs
  refreshing at once) without logging the user out.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.identity.roles import Role
from fm_common.identity.tokens import create_access_token, hash_refresh_token, new_refresh_token
from fm_common.logging import get_logger

from .config import settings
from .models import Household, RefreshToken, User
from .passwords import hash_password

log = get_logger(__name__)
REUSE_GRACE = timedelta(seconds=15)


class AuthError(Exception):
    def __init__(self, message: str, code: str = "invalid_grant") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class TokenPair:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    user: User


@dataclass(slots=True)
class ClientInfo:
    platform: str = "web"  # web | ios | android
    device_id: str | None = None
    user_agent: str | None = None
    ip: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


async def issue_tokens(db: AsyncSession, user: User, client: ClientInfo, family_id: uuid.UUID | None = None) -> TokenPair:
    raw, digest = new_refresh_token()
    expires = _now() + timedelta(days=settings.refresh_token_ttl_days)
    db.add(
        RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=digest,
            platform=client.platform,
            device_id=client.device_id,
            user_agent=(client.user_agent or "")[:512] or None,
            ip=client.ip if client.ip and client.ip != "unknown" else None,
            expires_at=expires,
        )
    )
    await db.flush()
    access, ttl = create_access_token(user.id, user.household_id, user.role.value)
    return TokenPair(access, ttl, raw, expires, user)


async def rotate_refresh_token(db: AsyncSession, raw: str, client: ClientInfo) -> TokenPair:
    token = (
        await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)).with_for_update()
        )
    ).scalar_one_or_none()
    if token is None:
        raise AuthError("Unknown refresh token")

    now = _now()
    if token.revoked_at is not None:
        if token.replaced_by is not None and now - token.revoked_at <= REUSE_GRACE:
            log.info("auth.refresh_race_absorbed", family_id=str(token.family_id))
        else:
            await revoke_family(db, token.family_id)
            await db.commit()
            log.warning("auth.refresh_token_reuse_detected", family_id=str(token.family_id), user_id=str(token.user_id))
            raise AuthError("Refresh token reuse detected; session revoked", code="token_reuse")
    if token.expires_at <= now:
        raise AuthError("Refresh token expired")

    user = await db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise AuthError("User disabled")

    pair = await issue_tokens(db, user, client, family_id=token.family_id)
    if token.revoked_at is None:
        token.revoked_at = now
        token.replaced_by = await db.scalar(
            select(RefreshToken.id).where(RefreshToken.token_hash == hash_refresh_token(pair.refresh_token))
        )
    return pair


async def revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def revoke_refresh_token(db: AsyncSession, raw: str) -> None:
    token = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
    ).scalar_one_or_none()
    if token:
        await revoke_family(db, token.family_id)


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "household"


async def create_household_with_owner(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    password: str | None,
    household_name: str | None = None,
    oidc_provider: str | None = None,
    oidc_subject: str | None = None,
    email_verified: bool = False,
) -> User:
    """Self-service sign-up creates a fresh household with the user as its OWNER."""
    name = household_name or f"{(full_name or email.split('@')[0]).split(' ')[0]}'s Family"
    household = Household(id=uuid.uuid4(), name=name, slug=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}")
    db.add(household)
    await db.flush()
    user = User(
        id=uuid.uuid4(),
        household_id=household.id,
        email=email.lower(),
        full_name=full_name,
        password_hash=await hash_password(password) if password else None,
        role=Role.OWNER,
        oidc_provider=oidc_provider,
        oidc_subject=oidc_subject,
        email_verified=email_verified,
    )
    db.add(user)
    await db.flush()
    return user
