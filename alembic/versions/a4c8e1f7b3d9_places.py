"""places and service points outside a floor

Revision ID: a4c8e1f7b3d9
Revises: e2b7d4a9c6f1
Create Date: 2026-10-30 10:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-30-locais-externos-design.md
§3 and §7: the `places` table, `service_points.place_id`, `service_points.floor_id` made nullable
and the `service_points_one_site` check (a point is in a place, or on a floor/room, never both).
Every existing point has a floor and no place, so the check holds for all of them.

Downgrade restores NOT NULL on `floor_id`, which is only possible while every point still has a
floor; it refuses, naming how many do not, instead of guessing a floor for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8e1f7b3d9"
down_revision: str | Sequence[str] | None = "e2b7d4a9c6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ONE_SITE = "service_points_one_site"


def upgrade() -> None:
    op.create_table(
        "places",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
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
        sa.UniqueConstraint("name"),
    )
    op.add_column("service_points", sa.Column("place_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_service_points_place_id_places", "service_points", "places", ["place_id"], ["id"]
    )
    op.alter_column("service_points", "floor_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        _ONE_SITE,
        "service_points",
        "place_id IS NULL OR (room_id IS NULL AND floor_id IS NULL)",
    )


def _refuse_if_a_point_has_no_floor() -> None:
    floorless = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM service_points WHERE floor_id IS NULL"))
        .scalar_one()
    )
    if floorless:
        raise RuntimeError(
            f"Cannot downgrade {revision}: {floorless} service point(s) have no floor "
            "(they sit in a place); move or delete them first, expected every service point "
            "to have a floor_id."
        )


def downgrade() -> None:
    _refuse_if_a_point_has_no_floor()
    op.drop_constraint(_ONE_SITE, "service_points", type_="check")
    op.alter_column("service_points", "floor_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint("fk_service_points_place_id_places", "service_points", type_="foreignkey")
    op.drop_column("service_points", "place_id")
    op.drop_table("places")
