"""platform: schemas, extensions, Immutable Audit Engine

Revision ID: 0001
Revises:
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = ("auth", "core", "market", "analytics", "advisor", "audit")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    for s in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")

    # ---------- Immutable Audit Engine (E33g) ----------
    op.execute("""
    CREATE TABLE audit.activity_logs (
        id              bigserial PRIMARY KEY,
        occurred_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
        txid            bigint      NOT NULL DEFAULT txid_current(),
        schema_name     varchar(63) NOT NULL,
        table_name      varchar(63) NOT NULL,
        operation       varchar(8)  NOT NULL,
        row_id          varchar(64),
        household_id    uuid,
        actor_user_id   uuid,
        trace_id        varchar(64),
        client_ip       inet,
        old_data        jsonb,
        new_data        jsonb,
        changed_fields  text[]
    )""")
    op.execute("CREATE INDEX ix_activity_logs_table_row ON audit.activity_logs (table_name, row_id)")
    op.execute("CREATE INDEX ix_activity_logs_household_time ON audit.activity_logs (household_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_activity_logs_actor ON audit.activity_logs (actor_user_id)")

    # Generic row-change trigger. TG_ARGV = columns to redact (secrets never enter the audit log).
    op.execute(r"""
    CREATE OR REPLACE FUNCTION audit.log_row_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    DECLARE
        old_j jsonb; new_j jsonb; changed text[]; redact text; hh uuid; rid text;
    BEGIN
        IF TG_OP IN ('UPDATE','DELETE') THEN old_j := to_jsonb(OLD); END IF;
        IF TG_OP IN ('INSERT','UPDATE') THEN new_j := to_jsonb(NEW); END IF;
        IF TG_NARGS > 0 THEN
            FOREACH redact IN ARRAY TG_ARGV LOOP
                IF old_j ? redact AND old_j->>redact IS NOT NULL THEN old_j := jsonb_set(old_j, ARRAY[redact], '"[REDACTED]"'); END IF;
                IF new_j ? redact AND new_j->>redact IS NOT NULL THEN new_j := jsonb_set(new_j, ARRAY[redact], '"[REDACTED]"'); END IF;
            END LOOP;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            SELECT array_agg(n.key ORDER BY n.key) INTO changed
            FROM jsonb_each(new_j) n LEFT JOIN jsonb_each(old_j) o USING (key)
            WHERE n.value IS DISTINCT FROM o.value AND n.key NOT IN ('updated_at', 'version');
            IF changed IS NULL THEN RETURN NEW; END IF;  -- no-op update: nothing to audit
        END IF;
        rid := COALESCE(new_j->>'id', old_j->>'id');
        hh := COALESCE(
            NULLIF(COALESCE(new_j->>'household_id', old_j->>'household_id'), '')::uuid,
            NULLIF(current_setting('app.household_id', true), '')::uuid);
        INSERT INTO audit.activity_logs (schema_name, table_name, operation, row_id, household_id, actor_user_id,
                                         trace_id, client_ip, old_data, new_data, changed_fields)
        VALUES (TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP, rid, hh,
                NULLIF(current_setting('app.user_id', true), '')::uuid,
                NULLIF(current_setting('app.trace_id', true), ''),
                NULLIF(current_setting('app.client_ip', true), '')::inet,
                old_j, new_j, changed);
        RETURN COALESCE(NEW, OLD);
    END $$""")

    # Append-only: the log itself can never be edited or erased.
    op.execute("""
    CREATE OR REPLACE FUNCTION audit.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit.activity_logs is append-only (% blocked)', TG_OP USING ERRCODE = 'insufficient_privilege';
    END $$""")
    op.execute("CREATE TRIGGER activity_logs_immutable BEFORE UPDATE OR DELETE ON audit.activity_logs FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation()")
    op.execute("CREATE TRIGGER activity_logs_no_truncate BEFORE TRUNCATE ON audit.activity_logs FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_mutation()")

    # updated_at safety net for raw-SQL writers (the ORM sets it too)
    op.execute("""
    CREATE OR REPLACE FUNCTION audit.touch_row() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        NEW.updated_at := now();
        RETURN NEW;
    END $$""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.activity_logs CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit.log_row_change() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit.forbid_mutation() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit.touch_row() CASCADE")
    for s in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
