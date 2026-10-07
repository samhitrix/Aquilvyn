"""PAN-tagged profiles: searchable PAN fingerprint + linked broker accounts

* ``core.profiles.pan_hash`` — keyed HMAC of the PAN (fm_common.crypto.pan_fingerprint), unique
  per household, so the PAN printed in a statement finds the right profile. Backfilled from the
  existing encrypted PANs (if two profiles share one, only the oldest keeps the tag).
* ``core.profile_accounts`` — broker client IDs (fingerprinted) linked to a profile.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("pan_hash", sa.String(length=64), nullable=True), schema="core")

    from fm_common.crypto import decrypt, pan_fingerprint

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, household_id, pan_encrypted FROM core.profiles WHERE pan_encrypted IS NOT NULL AND deleted_at IS NULL ORDER BY created_at"
    )).fetchall()
    taken: set[tuple[str, str]] = set()
    for pid, hid, enc in rows:
        try:
            fp = pan_fingerprint(decrypt(enc, aad=str(pid)))
        except Exception:
            continue  # undecryptable (key changed) — the owner can re-enter the PAN
        if (str(hid), fp) in taken:
            continue
        taken.add((str(hid), fp))
        conn.execute(sa.text("UPDATE core.profiles SET pan_hash = :fp WHERE id = :id"), {"fp": fp, "id": pid})

    op.create_index("uq_profiles_household_pan_hash", "profiles", ["household_id", "pan_hash"], unique=True, schema="core",
                    postgresql_where=sa.text("pan_hash IS NOT NULL AND deleted_at IS NULL"))

    op.create_table(
        "profile_accounts",
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["core.profiles.id"], name=op.f("fk_profile_accounts_profile_id_profiles"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_accounts")),
        sa.UniqueConstraint("household_id", "kind", "fingerprint", name="uq_profile_accounts_fp"),
        schema="core",
    )
    op.create_index(op.f("ix_core_profile_accounts_profile_id"), "profile_accounts", ["profile_id"], unique=False, schema="core")
    op.execute("CREATE TRIGGER audit_profile_accounts AFTER INSERT OR UPDATE OR DELETE ON core.profile_accounts "
               "FOR EACH ROW EXECUTE FUNCTION audit.log_row_change()")
    op.execute("CREATE TRIGGER touch_profile_accounts BEFORE UPDATE ON core.profile_accounts FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")


def downgrade() -> None:
    op.drop_table("profile_accounts", schema="core")
    op.drop_index("uq_profiles_household_pan_hash", table_name="profiles", schema="core")
    op.drop_column("profiles", "pan_hash", schema="core")
