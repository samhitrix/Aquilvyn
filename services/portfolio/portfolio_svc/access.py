"""Family & Profile Engine (E08) access rules: owners/admins/advisors see every profile in the
household; members/viewers see the profiles they're granted (their own 'Self' by default)."""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.deps import Principal
from fm_common.identity.rbac import P_PROFILE_READ_ANY, P_PROFILE_WRITE, P_PROFILE_WRITE_ANY

from .models import AccessLevel, Portfolio, Profile, ProfileAccess


async def visible_profile_ids(db: AsyncSession, p: Principal) -> list[uuid.UUID]:
    base = select(Profile.id).where(Profile.household_id == p.household_id, Profile.deleted_at.is_(None))
    if not p.can(P_PROFILE_READ_ANY):
        base = base.join(ProfileAccess, ProfileAccess.profile_id == Profile.id).where(ProfileAccess.user_id == p.user_id)
    return list((await db.execute(base)).scalars())


async def can_write_profile(db: AsyncSession, p: Principal, profile_id: uuid.UUID) -> bool:
    if p.can(P_PROFILE_WRITE_ANY):
        return True
    if not p.can(P_PROFILE_WRITE):
        return False
    lvl = await db.scalar(select(ProfileAccess.level).where(ProfileAccess.profile_id == profile_id, ProfileAccess.user_id == p.user_id))
    return lvl == AccessLevel.WRITE


async def get_profile(db: AsyncSession, p: Principal, profile_id: uuid.UUID, write: bool = False) -> Profile:
    prof = await db.get(Profile, profile_id)
    if prof is None or prof.household_id != p.household_id or prof.deleted_at is not None:
        raise HTTPException(404, "Profile not found")
    if profile_id not in await visible_profile_ids(db, p):
        raise HTTPException(404, "Profile not found")
    if write and not await can_write_profile(db, p, profile_id):
        raise HTTPException(403, "No write access to this profile")
    return prof


async def get_portfolio(db: AsyncSession, p: Principal, portfolio_id: uuid.UUID, write: bool = False) -> Portfolio:
    pf = await db.get(Portfolio, portfolio_id)
    if pf is None or pf.household_id != p.household_id or pf.deleted_at is not None:
        raise HTTPException(404, "Portfolio not found")
    await get_profile(db, p, pf.profile_id, write=write)
    return pf
