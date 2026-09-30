"""Async engine / session factory.

* asyncpg driver, pool sized for PgBouncer in *transaction* pooling mode.
* Under PgBouncer (transaction mode) server-side prepared statements can't be reused across
  transactions, so asyncpg's statement cache is disabled and statement names are randomised.
* Every transaction stamps ``app.user_id / app.household_id / app.trace_id / app.client_ip`` with
  ``set_config(..., is_local => true)`` so the audit trigger can attribute row mutations.
  ``is_local`` scopes the setting to the transaction, which is exactly what PgBouncer needs.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from fm_common.config import settings

# Populated by the request-context middleware (and by workers for background jobs).
audit_ctx: ContextVar[dict[str, str | None]] = ContextVar("audit_ctx", default={})  # noqa: B039 — never mutated; always replaced via .set(dict(...))


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if settings.db_behind_pgbouncer:
        kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    return kwargs


engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

_SET_AUDIT_CTX = text(
    "SELECT set_config('app.user_id', :user_id, true),"
    " set_config('app.household_id', :household_id, true),"
    " set_config('app.trace_id', :trace_id, true),"
    " set_config('app.client_ip', :client_ip, true)"
)


@event.listens_for(Session, "after_begin")
def _stamp_audit_context(session: Session, transaction: Any, connection: Any) -> None:
    ctx = audit_ctx.get()
    if not ctx:
        return
    connection.execute(
        _SET_AUDIT_CTX,
        {
            "user_id": ctx.get("user_id") or "",
            "household_id": ctx.get("household_id") or "",
            "trace_id": ctx.get("trace_id") or "",
            "client_ip": ctx.get("client_ip") or "",
        },
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by the handler."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping_db() -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
