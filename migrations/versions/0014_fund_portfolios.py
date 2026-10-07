"""Mutual-fund portfolio holdings from fund houses' monthly disclosures (MF look-through, E16)

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_portfolios",
        sa.Column("amfi_code", sa.String(length=16), nullable=False),
        sa.Column("amc", sa.String(length=120), nullable=False),
        sa.Column("scheme", sa.String(length=255), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("sheet", sa.String(length=255), nullable=True),
        sa.Column("match_score", sa.Numeric(precision=5, scale=3), nullable=True),
        sa.Column("equity_pct", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("total_pct", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("holdings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("amfi_code", name=op.f("pk_fund_portfolios")),
        schema="market",
    )


def downgrade() -> None:
    op.drop_table("fund_portfolios", schema="market")
