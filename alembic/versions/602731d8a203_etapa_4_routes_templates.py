"""etapa 4 routes templates

Revision ID: 602731d8a203
Revises: ae56651111fa
Create Date: 2026-08-31 11:59:53.439225

Hand-written from the Etapa 4 task-2 brief (autogenerate was noisy — the local PostGIS
image ships the tiger geocoder schema, which autogen wanted to drop). Ordering that matters:
the three new enum types are created before any table or column uses them; `route_templates`
is created before `routes.template_id`'s FK; `routes.route_date` is backfilled before its
NOT NULL constraint (spec §3.1 — no server default on the operational date).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "602731d8a203"
down_revision: str | Sequence[str] | None = "ae56651111fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: these are created (and dropped) explicitly below. A bare op.add_column
# with an Enum does NOT emit CREATE TYPE first (this bit Etapa 3, Task 6), and op.create_table
# WOULD emit it a second time for the shared `route_type` — explicit creation sidesteps both.
_route_type = postgresql.ENUM("REGULAR", "OCCASIONAL", name="route_type", create_type=False)
_route_status = postgresql.ENUM(
    "PLANNED", "IN_PROGRESS", "CANCELLED", "DONE", name="route_status", create_type=False
)
_template_recurrence = postgresql.ENUM(
    "DAILY", "WEEKLY", name="template_recurrence", create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    _route_type.create(bind, checkfirst=True)
    _route_status.create(bind, checkfirst=True)
    _template_recurrence.create(bind, checkfirst=True)

    # route_templates + route_template_stops first: routes.template_id FKs route_templates.id.
    op.create_table(
        "route_templates",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("field_worker_id", sa.UUID(), nullable=True),
        sa.Column("recurrence", _template_recurrence, nullable=False),
        sa.Column("weekdays", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("route_type", _route_type, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["field_worker_id"], ["field_workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "route_template_stops",
        sa.Column("route_template_id", sa.UUID(), nullable=False),
        sa.Column("service_point_id", sa.UUID(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("expected_arrival_from", sa.Time(), nullable=True),
        sa.Column("expected_arrival_to", sa.Time(), nullable=True),
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
        sa.ForeignKeyConstraint(["route_template_id"], ["route_templates.id"]),
        sa.ForeignKeyConstraint(["service_point_id"], ["service_points.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # routes.route_date: add nullable, backfill from scheduled_start_at (else today), then lock.
    op.add_column("routes", sa.Column("route_date", sa.Date(), nullable=True))
    op.execute("UPDATE routes SET route_date = COALESCE(scheduled_start_at::date, CURRENT_DATE)")
    op.alter_column("routes", "route_date", nullable=False)

    # route_type / status: server_default keeps existing rows valid, then it is dropped so the
    # ORM's Python-side default is the only source of truth going forward.
    op.add_column(
        "routes",
        sa.Column("route_type", _route_type, nullable=False, server_default="REGULAR"),
    )
    op.add_column(
        "routes",
        sa.Column("status", _route_status, nullable=False, server_default="PLANNED"),
    )
    op.execute("UPDATE routes SET status = 'IN_PROGRESS' WHERE started_at IS NOT NULL")
    op.alter_column("routes", "route_type", server_default=None)
    op.alter_column("routes", "status", server_default=None)

    op.add_column("routes", sa.Column("cancellation_reason", sa.String(length=300), nullable=True))
    op.add_column("routes", sa.Column("template_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "routes_template_id_fkey", "routes", "route_templates", ["template_id"], ["id"]
    )

    op.add_column("route_stops", sa.Column("distance_from_prev_m", sa.Float(), nullable=True))
    op.add_column("route_stops", sa.Column("duration_from_prev_s", sa.Float(), nullable=True))
    op.add_column(
        "route_stops",
        sa.Column("leg_geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("route_stops", "leg_geometry")
    op.drop_column("route_stops", "duration_from_prev_s")
    op.drop_column("route_stops", "distance_from_prev_m")

    op.drop_constraint("routes_template_id_fkey", "routes", type_="foreignkey")
    op.drop_column("routes", "template_id")
    op.drop_column("routes", "cancellation_reason")
    op.drop_column("routes", "status")
    op.drop_column("routes", "route_type")
    op.drop_column("routes", "route_date")

    op.drop_table("route_template_stops")
    op.drop_table("route_templates")

    bind = op.get_bind()
    _route_status.drop(bind, checkfirst=True)
    _template_recurrence.drop(bind, checkfirst=True)
    _route_type.drop(bind, checkfirst=True)
