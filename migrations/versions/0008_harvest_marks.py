"""Tax harvesting actions marked as done

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "harvest_marks",
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("fy", sa.String(length=7), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("booked", sa.Numeric(18, 2), nullable=False),
        sa.Column("st_part", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("lt_part", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("tax_saved", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["core.profiles.id"], name=op.f("fk_harvest_marks_profile_id_profiles"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_harvest_marks")),
        schema="core",
    )
    op.create_index(op.f("ix_core_harvest_marks_household_id"), "harvest_marks", ["household_id"], unique=False, schema="core")
    op.create_index("ix_core_harvest_marks_profile_fy", "harvest_marks", ["profile_id", "fy"], unique=False, schema="core")
    op.execute("CREATE TRIGGER touch_harvest_marks BEFORE UPDATE ON core.harvest_marks FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")


def downgrade() -> None:
    op.drop_table("harvest_marks", schema="core")
