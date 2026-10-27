"""execution complaints_suggestions

Revision ID: d8f2b6a4c1e7
Revises: c5e7a1d3b9f2
Create Date: 2026-10-26 11:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-26-exige-evidencias-
design.md §6.2: the free text a field worker types at check-out. TEXT NULL — blank input is
stored as NULL by the service. The downgrade drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f2b6a4c1e7"
down_revision: str | Sequence[str] | None = "c5e7a1d3b9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("complaints_suggestions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "complaints_suggestions")
