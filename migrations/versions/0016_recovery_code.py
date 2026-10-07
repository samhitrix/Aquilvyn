"""Users: a hashed one-time recovery code for "Forgot password?" (no email server needed)

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("recovery_hash", sa.String(length=255), nullable=True), schema="auth")


def downgrade() -> None:
    op.drop_column("users", "recovery_hash", schema="auth")
