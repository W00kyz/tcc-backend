"""workday start time

Revision ID: c5e8a2d4f6b1
Revises: b7d3f9e1c5a2
Create Date: 2026-11-05 09:00:00

Hand-written (project rule — no autogenerate). Spec
docs/specs/2026-11-05-banco-de-testes-horario-expediente-design.md §2: the workday window gets its
opening time, `system_settings.workday_start_time`, 07:00 by default (campus local time).

INTENTIONAL: `workday_end_time` is not touched. Its 17:00 default is already the real database's
closing time, and an end the admin edited must survive; the throwaway test database overwrites the
whole settings row in its own seed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e8a2d4f6b1"
down_revision: str | Sequence[str] | None = "b7d3f9e1c5a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default fills the existing singleton row, so the NOT NULL column needs no backfill.
    op.add_column(
        "system_settings",
        sa.Column("workday_start_time", sa.Time(), nullable=False, server_default="07:00"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("system_settings", "workday_start_time")
