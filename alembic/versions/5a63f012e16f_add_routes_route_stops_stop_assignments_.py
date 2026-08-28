"""add routes, route_stops, stop_assignments, executions and qr_scans

Revision ID: 5a63f012e16f
Revises: a8d113eb7d01
Create Date: 2026-08-28 16:51:50.592802

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a63f012e16f"
down_revision: str | Sequence[str] | None = "a8d113eb7d01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "routes",
        sa.Column("field_worker_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_latitude", sa.Float(), nullable=True),
        sa.Column("start_longitude", sa.Float(), nullable=True),
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
        "route_stops",
        sa.Column("route_id", sa.UUID(), nullable=False),
        sa.Column("service_point_id", sa.UUID(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("expected_arrival_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_arrival_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "DONE", name="route_stop_status"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"]),
        sa.ForeignKeyConstraint(["service_point_id"], ["service_points.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "executions",
        sa.Column("route_stop_id", sa.UUID(), nullable=False),
        sa.Column("field_worker_id", sa.UUID(), nullable=False),
        sa.Column("form_version_id", sa.UUID(), nullable=True),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.Enum("APP", "MANAGER_MANUAL", name="execution_source"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["route_stop_id"], ["route_stops.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "stop_assignments",
        sa.Column("route_stop_id", sa.UUID(), nullable=False),
        sa.Column("field_worker_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("assigned_by", sa.UUID(), nullable=False),
        sa.Column("transfer_reason", sa.String(length=300), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "EXECUTED", "IMPEDED", "REASSIGNED", "CANCELLED", name="stop_assignment_outcome"
            ),
            nullable=True,
        ),
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
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["field_worker_id"], ["field_workers.id"]),
        sa.ForeignKeyConstraint(["route_stop_id"], ["route_stops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "qr_scans",
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("qr_code_id", sa.UUID(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "geo_validation",
            sa.Enum("VALIDATED", "OUT_OF_RADIUS", "NOT_VALIDATED", name="geo_validation"),
            nullable=False,
        ),
        sa.Column("distance_m", sa.Float(), nullable=True),
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
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"]),
        sa.ForeignKeyConstraint(["qr_code_id"], ["qr_codes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("qr_scans")
    op.drop_table("stop_assignments")
    op.drop_table("executions")
    op.drop_table("route_stops")
    op.drop_table("routes")
