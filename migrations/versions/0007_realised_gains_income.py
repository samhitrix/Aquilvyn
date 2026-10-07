"""Tax P&L / capital-gains statements: realised sale lots + dividend / interest income

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _common() -> list[sa.Column]:
    return [
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("import_job_id", sa.UUID(), nullable=False),
        sa.Column("account_fp", sa.String(length=64), nullable=True),
        sa.Column("broker", sa.String(length=40), nullable=True),
        sa.Column("fy", sa.String(length=7), nullable=False),
        sa.Column("symbol", sa.String(length=160), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
    ]


def _tail(table: str) -> list[sa.Column | sa.Constraint]:
    return [
        sa.Column("superseded_by", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["core.profiles.id"], name=op.f(f"fk_{table}_profile_id_profiles"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_job_id"], ["core.import_jobs.id"], name=op.f(f"fk_{table}_import_job_id_import_jobs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    ]


def upgrade() -> None:
    op.create_table(
        "realised_gains", *_common(),
        sa.Column("asset", sa.String(length=12), nullable=False),
        sa.Column("term", sa.String(length=10), nullable=False),
        sa.Column("buy_date", sa.Date(), nullable=True),
        sa.Column("sell_date", sa.Date(), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("buy_value", sa.Numeric(18, 2), nullable=False),
        sa.Column("sell_value", sa.Numeric(18, 2), nullable=False),
        sa.Column("profit", sa.Numeric(18, 2), nullable=False),
        sa.Column("taxable_profit", sa.Numeric(18, 2), nullable=False),
        sa.Column("cost_override", sa.Numeric(18, 2), nullable=True),
        sa.Column("days_held", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=160), server_default="", nullable=False),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        *_tail("realised_gains"), schema="core",
    )
    op.create_table(
        "income_events", *_common(),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("per_unit", sa.Numeric(18, 6), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        *_tail("income_events"), schema="core",
    )
    for t in ("realised_gains", "income_events"):
        op.create_index(op.f(f"ix_core_{t}_household_id"), t, ["household_id"], unique=False, schema="core")
        op.create_index(op.f(f"ix_core_{t}_import_job_id"), t, ["import_job_id"], unique=False, schema="core")
        op.create_index(f"ix_core_{t}_profile_fy", t, ["profile_id", "fy"], unique=False, schema="core")
        op.execute(f"CREATE TRIGGER touch_{t} BEFORE UPDATE ON core.{t} FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")


def downgrade() -> None:
    for t in ("income_events", "realised_gains"):
        op.drop_table(t, schema="core")
