"""drop events and occasional points

Revision ID: c3e8a5d27f10
Revises: b7c4e2f9a1d3
Create Date: 2026-10-19 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3e8a5d27f10"
down_revision: str | Sequence[str] | None = "b7c4e2f9a1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_point_type_enum = postgresql.ENUM("REGULAR", "OCCASIONAL", name="point_type")


def upgrade() -> None:
    """Upgrade schema."""
    # The events feature (RF22, RF25, RF26) was removed: every service point is now a plain
    # point. Former occasional points stay, and the ones hidden as "archived" reappear.
    op.drop_constraint("service_points_event_id_fkey", "service_points", type_="foreignkey")
    op.drop_column("service_points", "event_id")
    op.drop_column("service_points", "point_type")
    _point_type_enum.drop(op.get_bind(), checkfirst=True)
    op.drop_table("events")


def downgrade() -> None:
    """Downgrade schema."""
    # Restores the shape only: the dropped events and point types are not recoverable, so
    # every point comes back REGULAR with no event.
    op.create_table(
        "events",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    _point_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "service_points",
        sa.Column("point_type", _point_type_enum, server_default="REGULAR", nullable=False),
    )
    op.add_column("service_points", sa.Column("event_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "service_points_event_id_fkey", "service_points", "events", ["event_id"], ["id"]
    )
