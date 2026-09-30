"""AI reviews: the reviewer's own suggested action and one-line verdict

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_reviews", sa.Column("suggested_action", sa.String(length=16), nullable=True), schema="advisor")
    op.add_column("ai_reviews", sa.Column("plain_verdict", sa.String(length=400), nullable=True), schema="advisor")
    op.add_column("ai_reviews", sa.Column("horizon", sa.String(length=12), nullable=True), schema="advisor")


def downgrade() -> None:
    op.drop_column("ai_reviews", "horizon", schema="advisor")
    op.drop_column("ai_reviews", "plain_verdict", schema="advisor")
    op.drop_column("ai_reviews", "suggested_action", schema="advisor")
