"""add building footprint

Revision ID: b7c4e2f9a1d3
Revises: 3cf7fec14642
Create Date: 2026-10-11 21:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7c4e2f9a1d3"
down_revision: str | Sequence[str] | None = "3cf7fec14642"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable on purpose: every existing building stays valid, just without an outline.
    op.add_column("buildings", sa.Column("footprint", postgresql.JSONB(), nullable=True))
    op.add_column("buildings", sa.Column("footprint_source", sa.String(20), nullable=True))
    op.add_column("buildings", sa.Column("osm_way_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("buildings", "osm_way_id")
    op.drop_column("buildings", "footprint_source")
    op.drop_column("buildings", "footprint")
