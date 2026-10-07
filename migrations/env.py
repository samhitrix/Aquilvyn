"""Alembic environment for the single PostgreSQL instance shared by all services.

Each service owns one schema and declares its own metadata; this env merges them so one
migration history keeps cross-cutting objects (schemas, audit triggers) consistent.
Migrations connect to Postgres *directly* (not PgBouncer): DDL + advisory locks need a
session-pooled connection."""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

ROOT = Path(__file__).resolve().parents[1]
for svc in ("identity", "portfolio", "market", "advisor", "readiness"):
    sys.path.insert(0, str(ROOT / "services" / svc))
sys.path.insert(0, str(ROOT / "libs" / "fm_common"))

from advisor_svc.models import Base as AdvisorBase  # noqa: E402
from identity_svc.models import Base as IdentityBase  # noqa: E402
from market_svc.models import Base as MarketBase  # noqa: E402
from portfolio_svc.models import Base as PortfolioBase  # noqa: E402
from readiness_svc.models import Base as ReadinessBase  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

SCHEMAS = ("auth", "core", "market", "analytics", "advisor", "readiness", "audit")
def _migrations_url() -> str:
    """Explicit override, else built from POSTGRES_* + MIGRATIONS_DB_HOST/PORT (or DB_HOST/PORT)."""
    from fm_common.config import build_database_url

    if url := os.environ.get("MIGRATIONS_DATABASE_URL"):
        return url
    e = os.environ
    return build_database_url(
        e.get("POSTGRES_USER", "aquilvyn"), e.get("POSTGRES_PASSWORD", "aquilvyn"),
        e.get("MIGRATIONS_DB_HOST") or e.get("DB_HOST", "localhost"), e.get("MIGRATIONS_DB_PORT") or e.get("DB_PORT", "5432"),
        e.get("POSTGRES_DB", "aquilvyn"),
    )


url = _migrations_url()
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # escape for configparser

target_metadata = MetaData()
for base in (IdentityBase, PortfolioBase, MarketBase, AdvisorBase, ReadinessBase):
    for table in base.metadata.tables.values():
        table.to_metadata(target_metadata)


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    if type_ == "schema":
        return name in SCHEMAS
    if type_ == "table" and name in ("activity_logs", "alembic_version"):
        return False  # managed by hand in 0001
    return True


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    # the version table lives in `audit`, so that schema must exist before Alembic looks for it
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS audit")
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="audit",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    connectable = async_engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, include_schemas=True, version_table_schema="audit")
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async())
