"""Column mappings remembered for broker file layouts Aquilvyn didn't recognise

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_templates",
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("uses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_templates")),
        sa.UniqueConstraint("household_id", "fingerprint", name="uq_import_templates_layout"),
        schema="core",
    )
    op.create_index(op.f("ix_core_import_templates_household_id"), "import_templates", ["household_id"], unique=False, schema="core")
    op.execute("CREATE TRIGGER touch_import_templates BEFORE UPDATE ON core.import_templates FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")


def downgrade() -> None:
    op.drop_table("import_templates", schema="core")
