"""attach Immutable Audit Engine + updated_at triggers to every service table

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# table -> columns redacted in the audit log (secrets / bulky blobs)
AUDITED: dict[str, tuple[str, ...]] = {
    "auth.households": (),
    "auth.users": ("password_hash",),
    "core.profiles": ("pan_encrypted",),
    "core.profile_access": (),
    "core.profile_groups": (),
    "core.portfolios": (),
    "core.transactions": (),
    "core.holding_prefs": (),
    "core.import_jobs": (),
    "market.instruments": (),
    "market.corporate_actions": (),
    "advisor.recommendations": ("short_report", "full_report", "narrative"),
    "advisor.ai_providers": ("api_key_encrypted",),
    "advisor.shadow_trades": (),
}
# High-volume / derived tables are deliberately NOT audited: auth.refresh_tokens, market.prices_eod,
# market.fundamentals, core.portfolio_snapshots, advisor.runs, advisor.ai_reviews, advisor.recommendation_outcomes.

TOUCHED = (
    "auth.households", "auth.users", "core.profiles", "core.profile_groups", "core.portfolios", "core.transactions",
    "core.holding_prefs", "core.import_jobs", "market.instruments", "market.corporate_actions",
    "advisor.recommendations", "advisor.ai_providers",
)


def _name(t: str) -> str:
    return t.split(".")[1]


def upgrade() -> None:
    for table, redact in AUDITED.items():
        args = ", ".join(f"'{c}'" for c in redact)
        op.execute(
            f"CREATE TRIGGER audit_{_name(table)} AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION audit.log_row_change({args})"
        )
    for table in TOUCHED:
        op.execute(f"CREATE TRIGGER touch_{_name(table)} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION audit.touch_row()")


def downgrade() -> None:
    for table in AUDITED:
        op.execute(f"DROP TRIGGER IF EXISTS audit_{_name(table)} ON {table}")
    for table in TOUCHED:
        op.execute(f"DROP TRIGGER IF EXISTS touch_{_name(table)} ON {table}")
