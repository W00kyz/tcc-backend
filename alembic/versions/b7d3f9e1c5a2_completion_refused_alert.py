"""completion refused alert kind

Revision ID: b7d3f9e1c5a2
Revises: a4c8e1f7b3d9
Create Date: 2026-10-30 12:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-30-locais-externos-design.md
§4: a refused completion by location warns the managers with a COMPLETION_REFUSED alert. The
Postgres enum `alert_kind` is shared by `alerts.kind` and `alert_rules.rule_kind`; the new value
is appended last. No `alert_rules` row is seeded: `get_alert_rule` / `list_alert_rules` create any
missing rule on first use, switched on, as they always have.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f9e1c5a2"
down_revision: str | Sequence[str] | None = "a4c8e1f7b3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres only lets a new enum value be used after the transaction that added it commits:
    # autocommit block (same as d4f1c2a9b8e7).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'COMPLETION_REFUSED'")


def downgrade() -> None:
    """Downgrade schema."""
    # INTENTIONAL no-op: Postgres cannot drop an enum value, and rebuilding `alert_kind` would
    # have to delete the COMPLETION_REFUSED alerts and rule. The value stays, unused by the older
    # code; upgrading again is safe because of IF NOT EXISTS.
