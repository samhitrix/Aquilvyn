"""Holding prefs: the person's own answer to "is a SIP running in this fund?" (overrides the guess from purchase dates)

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("holding_prefs", sa.Column("sip", sa.Boolean(), nullable=True), schema="core")


def downgrade() -> None:
    op.drop_column("holding_prefs", "sip", schema="core")
