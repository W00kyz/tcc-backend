"""floor levels, rooms and point activation

Revision ID: e2b7d4a9c6f1
Revises: d8f2b6a4c1e7
Create Date: 2026-10-29 10:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-29-andares-salas-design.md
§3-§7: `floors.level`, the `rooms` table, `service_points.room_id` and `.active`.

Backfill rules for the existing floors: label "Térreo" -> level 0; a label made only of digits
-> that number; any other label -> the next free levels above the building's current maximum,
in registration order (created_at, then id). No unique constraint, so two floors of a building
may end up on the same level. Every existing point gets a room named after it (a repeated name
in the same floor gets " (2)", " (3)"...) and every building without any floor gets a Térreo.

Downgrade drops rooms, `room_id`, `active` and `level` in the opposite order. The Térreo floors
inserted for floor-less buildings stay (their QR-less rows are harmless); that loss of
information is accepted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b7d4a9c6f1"
down_revision: str | Sequence[str] | None = "d8f2b6a4c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Labels the migration understands on its own. {1,4} keeps the cast far from integer overflow.
_NUMERIC_LABEL = "^[0-9]{1,4}$"

_BACKFILL_KNOWN_LEVELS = f"""
UPDATE floors SET level = CASE
    WHEN lower(btrim(label)) IN ('térreo', 'terreo') THEN 0
    ELSE label::integer
END
WHERE lower(btrim(label)) IN ('térreo', 'terreo') OR label ~ '{_NUMERIC_LABEL}'
"""

# The other labels stack up above the highest level already assigned in the building (-1 when
# the building has none, so the first one lands on 0), in registration order.
_BACKFILL_OTHER_LEVELS = f"""
WITH known AS (
    SELECT building_id, max(level) AS top
    FROM floors
    WHERE lower(btrim(label)) IN ('térreo', 'terreo') OR label ~ '{_NUMERIC_LABEL}'
    GROUP BY building_id
),
ranked AS (
    SELECT f.id,
           coalesce(k.top, -1) + row_number() OVER (
               PARTITION BY f.building_id ORDER BY f.created_at, f.id
           ) AS new_level
    FROM floors f
    LEFT JOIN known k ON k.building_id = f.building_id
    WHERE NOT (lower(btrim(f.label)) IN ('térreo', 'terreo') OR f.label ~ '{_NUMERIC_LABEL}')
)
UPDATE floors SET level = ranked.new_level FROM ranked WHERE floors.id = ranked.id
"""

_BACKFILL_ROOMS = """
INSERT INTO rooms (id, floor_id, name)
SELECT gen_random_uuid(),
       floor_id,
       CASE WHEN n = 1 THEN name ELSE name || ' (' || n || ')' END
FROM (
    SELECT floor_id, name,
           row_number() OVER (PARTITION BY floor_id, name ORDER BY created_at, id) AS n
    FROM service_points
) numbered
"""

# Matches each point to the room created for it, using the same numbering as the insert above.
_LINK_POINTS_TO_ROOMS = """
UPDATE service_points SET room_id = rooms.id
FROM (
    SELECT id,
           floor_id,
           CASE WHEN n = 1 THEN name ELSE name || ' (' || n || ')' END AS room_name
    FROM (
        SELECT id, floor_id, name,
               row_number() OVER (PARTITION BY floor_id, name ORDER BY created_at, id) AS n
        FROM service_points
    ) numbered
) named
JOIN rooms ON rooms.floor_id = named.floor_id AND rooms.name = named.room_name
WHERE service_points.id = named.id
"""

_ADD_MISSING_GROUND_FLOORS = """
INSERT INTO floors (id, building_id, label, level)
SELECT gen_random_uuid(), b.id, 'Térreo', 0
FROM buildings b
WHERE NOT EXISTS (SELECT 1 FROM floors f WHERE f.building_id = b.id)
"""


def upgrade() -> None:
    op.add_column(
        "floors", sa.Column("level", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.execute(_BACKFILL_KNOWN_LEVELS)
    op.execute(_BACKFILL_OTHER_LEVELS)

    op.create_table(
        "rooms",
        sa.Column("floor_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
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
        sa.ForeignKeyConstraint(["floor_id"], ["floors.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("floor_id", "name"),
    )
    op.add_column("service_points", sa.Column("room_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_service_points_room_id_rooms", "service_points", "rooms", ["room_id"], ["id"]
    )
    op.add_column(
        "service_points",
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.execute(_BACKFILL_ROOMS)
    op.execute(_LINK_POINTS_TO_ROOMS)
    op.execute(_ADD_MISSING_GROUND_FLOORS)


def downgrade() -> None:
    op.drop_column("service_points", "active")
    op.drop_constraint("fk_service_points_room_id_rooms", "service_points", type_="foreignkey")
    op.drop_column("service_points", "room_id")
    op.drop_table("rooms")
    op.drop_column("floors", "level")
