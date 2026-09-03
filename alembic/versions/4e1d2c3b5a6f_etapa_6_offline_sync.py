"""etapa 6 offline sync

Revision ID: 4e1d2c3b5a6f
Revises: 2c8491d45546
Create Date: 2026-09-03 09:00:00.000000

Hand-written (project rule — no autogenerate). Schema groundwork for the offline mode:
`executions.clock_skew_seconds` records the app-reported device/server clock offset seen at
sync time (spec Ruling 7 — drives the CLOCK_SKEW review flag), and
`evidence_items.idempotency_key` lets the sync worker retry an evidence upload without
creating a duplicate row. The unique index is created explicitly so `downgrade()` can drop
it by name before the column goes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4e1d2c3b5a6f"
down_revision: str | Sequence[str] | None = "2c8491d45546"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("executions", sa.Column("clock_skew_seconds", sa.Float(), nullable=True))
    op.add_column(
        "evidence_items",
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_items_idempotency_key",
        "evidence_items",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_evidence_items_idempotency_key", table_name="evidence_items")
    op.drop_column("evidence_items", "idempotency_key")
    op.drop_column("executions", "clock_skew_seconds")
