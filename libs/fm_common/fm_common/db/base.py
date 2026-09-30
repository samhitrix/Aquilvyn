from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def make_base() -> type[DeclarativeBase]:
    """Each service gets its *own* declarative Base/metadata: it only knows its own tables.
    Cross-service references are plain UUID columns (no cross-schema FKs), which is what lets
    each service evolve and deploy independently."""

    class Base(DeclarativeBase):
        metadata = MetaData(naming_convention=NAMING)
        # Fetch server-side defaults (updated_at = now(), created_at, …) via RETURNING at flush
        # time, so reading them after commit never triggers lazy IO in async code.
        __mapper_args__ = {"eager_defaults": True}

    return Base


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"), default=uuid.uuid4
    )


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Also maintained by the platform.touch_row() trigger so raw SQL writes stay correct.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SyncTracked(Timestamps):
    """Soft-delete + version: tombstones for the (P4) mobile delta-sync engine and
    optimistic-concurrency for the web."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(server_default=text("1"), default=1, nullable=False)
