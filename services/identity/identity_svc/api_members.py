"""Household member management (owner/admin)."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, text

from fm_common.deps import DB, CurrentUser, Principal, require
from fm_common.events import publish
from fm_common.identity.rbac import P_AUDIT_READ, P_HOUSEHOLD_MANAGE, P_MEMBERS_MANAGE
from fm_common.identity.roles import Role

from .models import Household, User
from .passwords import hash_password
from .tokens_service import revoke_all_for_user

router = APIRouter(tags=["household"])


class MemberOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None


class MemberIn(BaseModel):
    email: EmailStr
    full_name: str = Field(max_length=200)
    role: Role = Role.MEMBER
    temporary_password: str = Field(min_length=10, max_length=128)


class MemberPatch(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


class HouseholdOut(BaseModel):
    id: uuid.UUID
    name: str
    plan: str
    settings: dict


class HouseholdPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    settings: dict | None = None


def _out(u: User) -> MemberOut:
    return MemberOut(id=u.id, email=u.email, full_name=u.full_name, role=u.role.value, is_active=u.is_active, last_login_at=u.last_login_at)


@router.get("/household", response_model=HouseholdOut)
async def get_household(principal: CurrentUser, db: DB) -> HouseholdOut:
    h = await db.get(Household, principal.household_id)
    if h is None:
        raise HTTPException(404, "Household not found")
    return HouseholdOut(id=h.id, name=h.name, plan=h.plan, settings=h.settings)


@router.patch("/household", response_model=HouseholdOut)
async def patch_household(body: HouseholdPatch, db: DB, principal: Principal = require(P_HOUSEHOLD_MANAGE)) -> HouseholdOut:
    h = await db.get(Household, principal.household_id)
    if h is None:
        raise HTTPException(404, "Household not found")
    if body.name is not None:
        h.name = body.name
    if body.settings is not None:
        h.settings = {**h.settings, **body.settings}
    await db.commit()
    return HouseholdOut(id=h.id, name=h.name, plan=h.plan, settings=h.settings)


@router.get("/members", response_model=list[MemberOut])
async def list_members(principal: CurrentUser, db: DB) -> list[MemberOut]:
    rows = (await db.execute(select(User).where(User.household_id == principal.household_id).order_by(User.created_at))).scalars()
    return [_out(u) for u in rows]


@router.post("/members", response_model=MemberOut, status_code=201)
async def add_member(body: MemberIn, db: DB, principal: Principal = require(P_MEMBERS_MANAGE)) -> MemberOut:
    if body.role in (Role.OWNER, Role.SERVICE):
        raise HTTPException(400, "Cannot assign this role")
    if await db.scalar(select(User.id).where(func.lower(User.email) == body.email.lower())):
        raise HTTPException(409, "Email already registered")
    user = User(
        id=uuid.uuid4(), household_id=principal.household_id, email=body.email.lower(), full_name=body.full_name,
        role=body.role, password_hash=await hash_password(body.temporary_password),
    )
    db.add(user)
    await db.commit()
    await publish(
        "member.added",
        {"user_id": str(user.id), "full_name": user.full_name, "role": user.role.value},
        household_id=str(principal.household_id),
    )
    return _out(user)


@router.patch("/members/{member_id}", response_model=MemberOut)
async def patch_member(member_id: uuid.UUID, body: MemberPatch, db: DB, principal: Principal = require(P_MEMBERS_MANAGE)) -> MemberOut:
    user = await db.get(User, member_id)
    if user is None or user.household_id != principal.household_id:
        raise HTTPException(404, "Member not found")
    if user.role == Role.OWNER and user.id != principal.user_id:
        raise HTTPException(403, "The owner can only be changed by the owner")
    if body.role is not None:
        if body.role in (Role.OWNER, Role.SERVICE):
            raise HTTPException(400, "Cannot assign this role")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            await revoke_all_for_user(db, user.id)
    await db.commit()
    return _out(user)


# ---------------- Immutable Audit Engine (E33g) read API ----------------
class AuditRow(BaseModel):
    id: int
    occurred_at: datetime
    schema_name: str
    table_name: str
    operation: str
    row_id: str | None
    actor_user_id: uuid.UUID | None
    trace_id: str | None
    changed_fields: list[str] | None
    old_data: dict | None
    new_data: dict | None


@router.get("/audit", response_model=list[AuditRow])
async def audit_log(
    db: DB,
    principal: Principal = require(P_AUDIT_READ),
    table: str | None = None,
    row_id: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
) -> list[AuditRow]:
    sql = """
        SELECT id, occurred_at, schema_name, table_name, operation, row_id, actor_user_id, trace_id,
               changed_fields, old_data, new_data
        FROM audit.activity_logs
        WHERE household_id = :hid
          AND (CAST(:table AS text) IS NULL OR table_name = :table)
          AND (CAST(:row_id AS text) IS NULL OR row_id = :row_id)
          AND (CAST(:before AS bigint) IS NULL OR id < :before)
        ORDER BY id DESC LIMIT :limit
    """
    rows = await db.execute(
        text(sql),
        {"hid": principal.household_id, "table": table, "row_id": row_id, "before": before_id, "limit": min(limit, 500)},
    )
    return [AuditRow(**r._mapping) for r in rows]
