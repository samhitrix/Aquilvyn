"""AI providers: priority order (fallback chain)

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_providers", sa.Column("priority", sa.Integer(), server_default="100", nullable=False), schema="advisor")
    # keep today's behaviour: the primary provider first
    op.execute("UPDATE advisor.ai_providers SET priority = CASE WHEN is_primary THEN 1 ELSE 100 END")


def downgrade() -> None:
    op.drop_column("ai_providers", "priority", schema="advisor")
