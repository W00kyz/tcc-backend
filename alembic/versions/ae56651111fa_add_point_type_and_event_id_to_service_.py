"""add point_type and event_id to service_points

Revision ID: ae56651111fa
Revises: 14d70a21e565
Create Date: 2026-08-30 11:15:51.017759

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ae56651111fa"
down_revision: str | Sequence[str] | None = "14d70a21e565"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_point_type_enum = postgresql.ENUM("REGULAR", "OCCASIONAL", name="point_type")


def upgrade() -> None:
    """Upgrade schema."""
    # ADD COLUMN with an Enum type does not auto-create the Postgres type the way
    # create_table does — the type has to exist before the ALTER TABLE runs.
    _point_type_enum.create(op.get_bind(), checkfirst=True)
    # server_default keeps the rows seeded in Etapa 2 valid: every existing service_point
    # becomes REGULAR without a data-migration step.
    op.add_column(
        "service_points",
        sa.Column("point_type", _point_type_enum, server_default="REGULAR", nullable=False),
    )
    op.add_column("service_points", sa.Column("event_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "service_points_event_id_fkey", "service_points", "events", ["event_id"], ["id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("service_points_event_id_fkey", "service_points", type_="foreignkey")
    op.drop_column("service_points", "event_id")
    op.drop_column("service_points", "point_type")
    _point_type_enum.drop(op.get_bind(), checkfirst=True)
