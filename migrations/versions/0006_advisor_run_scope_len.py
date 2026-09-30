"""advisor.runs.scope / trigger: 40 → 64 chars

A per-person run stores ``profile:<uuid>`` (44 chars), which overflowed varchar(40) and failed
every "Run advisor" started for a single profile.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("scope", "trigger"):
        op.alter_column("runs", col, type_=sa.String(length=64), existing_type=sa.String(length=40), schema="advisor")


def downgrade() -> None:
    for col in ("scope", "trigger"):
        op.alter_column("runs", col, type_=sa.String(length=40), existing_type=sa.String(length=64), schema="advisor")
