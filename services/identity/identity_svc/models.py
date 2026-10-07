from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fm_common.db.base import Timestamps, UUIDPk, make_base
from fm_common.identity.roles import Role

Base = make_base()
SCHEMA = "auth"


class Household(UUIDPk, Timestamps, Base):
    """The tenant. A family (or an advisor's client book in P3)."""

    __tablename__ = "households"
    __table_args__ = {"schema": SCHEMA}

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    plan: Mapped[str] = mapped_column(String(40), server_default="free")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    settings: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class User(UUIDPk, Timestamps, Base):
    """A *member* of a household who can log in. Investing entities are Profiles (portfolio-svc)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_provider", "oidc_subject", name="uq_users_oidc_identity"),
        Index("ix_users_email_lower", text("lower(email)"), unique=True),
        {"schema": SCHEMA},
    )

    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.households.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Argon2 hash of the one-time recovery code ("Forgot password?" without email); None = not created yet
    recovery_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200), server_default="")
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", schema=SCHEMA, values_callable=lambda e: [m.value for m in e]),
        server_default=Role.MEMBER.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    email_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    oidc_provider: Mapped[str | None] = mapped_column(String(50))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    household: Mapped[Household] = relationship(lazy="joined")


class RefreshToken(UUIDPk, Base):
    """Opaque refresh tokens stored only as SHA-256 hashes, grouped in rotation families."""

    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    platform: Mapped[str] = mapped_column(String(16), server_default="web")
    device_id: Mapped[str | None] = mapped_column(String(128))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(INET)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
