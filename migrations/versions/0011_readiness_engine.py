"""Readiness engine: per-holding checks and the passes that produced them

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS readiness")
    op.create_table(
        "holding_checks",
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=True),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("rec_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("asset_type", sa.String(length=24), nullable=False),
        sa.Column("profile_name", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("fixes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holding_checks")),
        sa.UniqueConstraint("household_id", "profile_id", "instrument_id", name="uq_holding_checks_holding"),
        schema="readiness",
    )
    op.create_index("ix_readiness_holding_checks_household", "holding_checks", ["household_id"], unique=False, schema="readiness")
    op.execute("CREATE TRIGGER touch_holding_checks BEFORE UPDATE ON readiness.holding_checks FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")
    op.create_table(
        "sweeps",
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sweeps")),
        schema="readiness",
    )
    op.create_index("ix_readiness_sweeps_household_started", "sweeps", ["household_id", "started_at"], unique=False, schema="readiness")


def downgrade() -> None:
    op.drop_table("sweeps", schema="readiness")
    op.drop_table("holding_checks", schema="readiness")
    op.execute("DROP SCHEMA IF EXISTS readiness")
