"""Profiles: optional email and mobile number

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("email", sa.String(length=254), nullable=True), schema="core")
    op.add_column("profiles", sa.Column("mobile", sa.String(length=20), nullable=True), schema="core")


def downgrade() -> None:
    op.drop_column("profiles", "mobile", schema="core")
    op.drop_column("profiles", "email", schema="core")
