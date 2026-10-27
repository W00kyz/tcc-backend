"""appeals over released stops

Revision ID: c6e1f8a2b4d9
Revises: a1c5e9b3d7f2
Create Date: 2026-10-20 21:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-20-alocacao-automatica-
design.md §5.4: a worker contests a stop released while they were working it; the manager
accepts or rejects. `route_stop_id` is UNIQUE — one appeal per released stop — and the app's
`idempotency_key` makes a retried filing land on the same row. `execution_source` already has
`APPEAL` since the allocation reset.

The two enums are used by this one table only, so `create_table` creates them; the downgrade
drops them explicitly (`drop_table` does not).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c6e1f8a2b4d9"
down_revision: str | Sequence[str] | None = "a1c5e9b3d7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "appeals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "route_stop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("route_stops.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "field_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_workers.id"),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum("NO_SIGNAL", "LEFT_BRIEFLY", "APP_CLOSED", "OTHER", name="appeal_reason"),
            nullable=False,
        ),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("claimed_executed_at", sa.DateTime(timezone=True), nullable=True),
        # No server_default: PENDING comes from the ORM default (fresh table, nothing to fill).
        sa.Column(
            "status",
            sa.Enum("PENDING", "ACCEPTED", "REJECTED", name="appeal_status"),
            nullable=False,
        ),
        sa.Column(
            "reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(1000), nullable=True),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # The worker's "my appeals" filters by worker; the manager's queue by status.
    op.create_index("ix_appeals_field_worker_id", "appeals", ["field_worker_id"])
    op.create_index("ix_appeals_status", "appeals", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_appeals_status", table_name="appeals")
    op.drop_index("ix_appeals_field_worker_id", table_name="appeals")
    op.drop_table("appeals")

    bind = op.get_bind()
    postgresql.ENUM(name="appeal_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="appeal_reason").drop(bind, checkfirst=True)
