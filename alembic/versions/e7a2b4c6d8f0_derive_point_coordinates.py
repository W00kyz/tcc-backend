"""derive service point coordinates from the building outline

Revision ID: e7a2b4c6d8f0
Revises: d4f1c2a9b8e7
Create Date: 2026-10-20 10:00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a2b4c6d8f0"
down_revision: str | Sequence[str] | None = "d4f1c2a9b8e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # A point has no location of its own any more: it is at the centre of its building's outline
    # (app/domain/catalog/points.py keeps it so from now on). Points of a legacy building with no
    # outline keep their coordinates until the building is drawn.
    op.execute(
        """
        UPDATE service_points AS point
        SET longitude = ST_X(centre.geom), latitude = ST_Y(centre.geom)
        FROM (
            SELECT floors.id AS floor_id,
                   ST_Centroid(ST_GeomFromGeoJSON(buildings.footprint::text)) AS geom
            FROM floors JOIN buildings ON buildings.id = floors.building_id
            WHERE buildings.footprint IS NOT NULL
        ) AS centre
        WHERE point.floor_id = centre.floor_id
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The coordinates each point had before cannot be recovered; the derived ones stay.
