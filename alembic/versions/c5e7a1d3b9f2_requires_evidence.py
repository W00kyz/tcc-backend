"""requires_evidence on service types and route stops

Revision ID: c5e7a1d3b9f2
Revises: a9d2c4e6f1b3
Create Date: 2026-10-26 10:00:00

Hand-written (project rule — no autogenerate). Spec docs/specs/2026-10-26-exige-evidencias-
design.md §6: a service type says whether finishing one of its stops needs evidence, and each
stop keeps a copy taken at allocation. Both columns are BOOLEAN NOT NULL DEFAULT false.

Backfill: the type named "Limpeza" (the one the seed creates) starts requiring evidence, every
other type does not; existing stops take their type's value, a stop without a type gets false.
The downgrade drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e7a1d3b9f2"
down_revision: str | Sequence[str] | None = "a9d2c4e6f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("service_types", "route_stops"):
        op.add_column(
            table,
            sa.Column(
                "requires_evidence", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
    op.execute("UPDATE service_types SET requires_evidence = true WHERE name = 'Limpeza'")
    op.execute(
        "UPDATE route_stops rs SET requires_evidence = st.requires_evidence "
        "FROM service_types st WHERE rs.service_type_id = st.id"
    )


def downgrade() -> None:
    op.drop_column("route_stops", "requires_evidence")
    op.drop_column("service_types", "requires_evidence")
