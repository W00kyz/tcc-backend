"""etapa 8 impediments

Revision ID: 1212c573b12e
Revises: 7f3a1b9c2d84
Create Date: 2026-09-04 19:14:01.093723

Hand-written (project rule — no autogenerate). Creates the `impediments` table (RF16-RF21,
Etapa 8) — the field worker's "I cannot execute this" request. It never touches
`stop_assignments`: a manager-approved reassignment appends a new `StopAssignment` row there
instead (spec §3 Ruling 2 / `app/domain/routing/models.py`).

Each of the three enums here is used by exactly one column on this one table, so — unlike the
Etapa 7 migration's shared `form_version_status`/`question_type` — they are created and dropped
implicitly by `sa.Enum(..., create_type=True)` (the default) via `create_table`/`drop_table`,
no explicit `.create()`/`.drop()` calls needed.

Indexes on `route_id` and `status`: the manager's queue filters by `status`, and the
reassignment flow lists impediments by route.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1212c573b12e"
down_revision: str | Sequence[str] | None = "7f3a1b9c2d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "impediments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "route_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routes.id"),
            nullable=False,
        ),
        sa.Column(
            "route_stop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("route_stops.id"),
            nullable=True,
        ),
        sa.Column(
            "scope",
            sa.Enum("STOP", "ROUTE_REMAINDER", name="impediment_scope", create_type=True),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(
                "ACCESS_BLOCKED",
                "SITE_REFUSAL",
                "HEALTH_ISSUE",
                "OTHER",
                name="impediment_reason",
                create_type=True,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "reported_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_workers.id"),
            nullable=False,
        ),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # No server_default: PENDING is applied by the ORM's mapped_column default at
        # INSERT time (this is a fresh table, nothing to backfill).
        sa.Column(
            "status",
            sa.Enum("PENDING", "RESOLVED", name="impediment_status", create_type=True),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
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
    )
    op.create_index("ix_impediments_route_id", "impediments", ["route_id"])
    op.create_index("ix_impediments_status", "impediments", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_impediments_status", table_name="impediments")
    op.drop_index("ix_impediments_route_id", table_name="impediments")
    op.drop_table("impediments")

    bind = op.get_bind()
    postgresql.ENUM(name="impediment_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="impediment_reason").drop(bind, checkfirst=True)
    postgresql.ENUM(name="impediment_scope").drop(bind, checkfirst=True)
