"""merge duplicate 'Self' profiles created by the first-login race; one live profile per member

Before this, the ``member.registered`` consumer and the first ``/profiles`` call could both
bootstrap a profile for the same member (one named after them, one called "Me"). This keeps
one — the real-name one, else the oldest — moves everything the duplicate owned onto it, and
soft-deletes the duplicate. Then a partial unique index stops it from happening again.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

MERGE = r"""
DO $$
DECLARE
  d record;          -- one duplicate profile + its keeper
  pf record;         -- a portfolio of the duplicate
  target uuid;
BEGIN
  FOR d IN
    WITH ranked AS (
      SELECT id, household_id, linked_user_id,
             first_value(id) OVER w AS keeper_id,
             row_number() OVER w AS rn
      FROM core.profiles
      WHERE linked_user_id IS NOT NULL AND deleted_at IS NULL
      WINDOW w AS (PARTITION BY household_id, linked_user_id
                   ORDER BY (display_name = 'Me'), created_at, id)
    )
    SELECT id AS dup_id, keeper_id FROM ranked WHERE rn > 1
  LOOP
    -- portfolios: merge into the keeper's portfolio of the same kind, else move across
    FOR pf IN SELECT * FROM core.portfolios WHERE profile_id = d.dup_id AND deleted_at IS NULL LOOP
      SELECT id INTO target FROM core.portfolios
       WHERE profile_id = d.keeper_id AND kind = pf.kind AND deleted_at IS NULL
       ORDER BY created_at LIMIT 1;
      IF target IS NULL THEN
        UPDATE core.portfolios SET profile_id = d.keeper_id WHERE id = pf.id;
      ELSE
        -- same row imported into both → keep the keeper's copy
        UPDATE core.transactions t SET deleted_at = now()
         WHERE t.portfolio_id = pf.id AND t.deleted_at IS NULL AND t.client_ref IS NOT NULL
           AND EXISTS (SELECT 1 FROM core.transactions k WHERE k.portfolio_id = target AND k.client_ref = t.client_ref);
        UPDATE core.transactions t SET client_ref = NULL
         WHERE t.portfolio_id = pf.id AND t.client_ref IS NOT NULL
           AND EXISTS (SELECT 1 FROM core.transactions k WHERE k.portfolio_id = target AND k.client_ref = t.client_ref);
        UPDATE core.transactions SET portfolio_id = target WHERE portfolio_id = pf.id;
        UPDATE core.import_jobs SET portfolio_id = target WHERE portfolio_id = pf.id;
        UPDATE core.applied_corporate_actions a SET portfolio_id = target
         WHERE a.portfolio_id = pf.id
           AND NOT EXISTS (SELECT 1 FROM core.applied_corporate_actions b WHERE b.portfolio_id = target AND b.corporate_action_id = a.corporate_action_id);
        DELETE FROM core.applied_corporate_actions WHERE portfolio_id = pf.id;
        DELETE FROM core.portfolio_snapshots WHERE portfolio_id = pf.id;  -- rebuilt nightly
        UPDATE core.portfolios SET deleted_at = now() WHERE id = pf.id;
      END IF;
    END LOOP;

    -- holding preferences (intent, thesis, stops): keep the keeper's where both exist
    UPDATE core.holding_prefs h SET profile_id = d.keeper_id
     WHERE h.profile_id = d.dup_id
       AND NOT EXISTS (SELECT 1 FROM core.holding_prefs k WHERE k.profile_id = d.keeper_id AND k.instrument_id = h.instrument_id);
    UPDATE core.holding_prefs SET deleted_at = now() WHERE profile_id = d.dup_id AND deleted_at IS NULL;

    -- access grants and groups
    INSERT INTO core.profile_access (profile_id, user_id, level)
      SELECT d.keeper_id, user_id, level FROM core.profile_access WHERE profile_id = d.dup_id
      ON CONFLICT DO NOTHING;
    DELETE FROM core.profile_access WHERE profile_id = d.dup_id;
    UPDATE core.profile_groups
       SET profile_ids = ARRAY(SELECT DISTINCT unnest(array_replace(profile_ids, d.dup_id, d.keeper_id)))
     WHERE d.dup_id = ANY(profile_ids);

    UPDATE core.profiles SET deleted_at = now() WHERE id = d.dup_id;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(MERGE)
    op.create_index(
        "uq_profiles_household_linked_user", "profiles", ["household_id", "linked_user_id"], unique=True, schema="core",
        postgresql_where="linked_user_id IS NOT NULL AND deleted_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_profiles_household_linked_user", table_name="profiles", schema="core")
