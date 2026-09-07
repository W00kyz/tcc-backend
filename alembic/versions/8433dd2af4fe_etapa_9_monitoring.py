"""etapa 9 monitoring

Revision ID: 8433dd2af4fe
Revises: 1212c573b12e
Create Date: 2026-09-06 00:00:00.000000

Hand-written (project rule — no autogenerate). Creates the four Etapa 9 tables (`alerts`,
`alert_rules`, `notifications`, `worker_positions`), their indexes, the six new
`system_settings` tolerance/cadence columns, and seeds one `alert_rules` row per rule kind.

`alert_kind` is shared by `alerts.kind` AND `alert_rules.rule_kind`, so — like the Etapa 7
`form_version_status` — it is created and dropped explicitly with `create_type=False`;
a second `create_table` would otherwise emit `CREATE TYPE` again and fail. `alert_severity`
and `alert_status` each back a single column, so `create_type=True` (the default) lets
`create_table` / `drop_table` manage them.

The six `system_settings` columns land with a `server_default` (so the existing singleton row
gets a value) which is then dropped — the ORM's `mapped_column(default=...)` owns new rows
from here on (Etapa 6 pattern, revision 4e1d2c3b5a6f).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8433dd2af4fe"
down_revision: str | Sequence[str] | None = "1212c573b12e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALERT_KIND_VALUES = ("LATE_START", "WORKER_ABSENT", "LATE_STOP", "NO_EVIDENCE")

# Shared enum — created/dropped explicitly (see module docstring).
_alert_kind = postgresql.ENUM(*_ALERT_KIND_VALUES, name="alert_kind", create_type=False)

# (name, server_default) for the six new system_settings columns.
_SETTINGS_COLUMNS = (
    ("alert_scan_interval_seconds", "60"),
    ("late_start_tolerance_min", "15"),
    ("absence_tolerance_min", "30"),
    ("late_stop_tolerance_min", "15"),
    ("no_evidence_grace_min", "30"),
    ("position_report_interval_seconds", "60"),
)


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    """Fresh created_at/updated_at columns (a Column instance belongs to one table only)."""
    return (
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


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    _alert_kind.create(bind, checkfirst=True)

    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", _alert_kind, nullable=False),
        sa.Column(
            "severity",
            sa.Enum("INFO", "WARNING", "CRITICAL", name="alert_severity", create_type=True),
            nullable=False,
        ),
        sa.Column("dedup_key", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=False),
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
            "field_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_workers.id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum("OPEN", "ACKNOWLEDGED", "RESOLVED", name="alert_status", create_type=True),
            nullable=False,
        ),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
    )
    op.create_index("ix_alerts_status", "alerts", ["status"])
    op.create_index("ix_alerts_route_id", "alerts", ["route_id"])

    op.create_table(
        "alert_rules",
        sa.Column("rule_kind", _alert_kind, primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("threshold_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.bulk_insert(
        sa.table(
            "alert_rules",
            sa.column("rule_kind", _alert_kind),
            sa.column("enabled", sa.Boolean()),
            sa.column("threshold_minutes", sa.Integer()),
        ),
        [
            {"rule_kind": kind, "enabled": True, "threshold_minutes": None}
            for kind in _ALERT_KIND_VALUES
        ],
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
    )
    op.alter_column("notifications", "data", server_default=None)
    op.create_index("ix_notifications_user_id_read_at", "notifications", ["user_id", "read_at"])
    op.create_index(
        "ix_notifications_user_id_created_at", "notifications", ["user_id", "created_at"]
    )

    op.create_table(
        "worker_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "field_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_workers.id"),
            nullable=False,
        ),
        sa.Column(
            "route_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routes.id"),
            nullable=False,
        ),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        *_timestamp_columns(),
    )
    op.create_index(
        "ix_worker_positions_field_worker_id_reported_at",
        "worker_positions",
        ["field_worker_id", sa.text("reported_at DESC")],
    )

    for name, default in _SETTINGS_COLUMNS:
        op.add_column(
            "system_settings",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.alter_column("system_settings", name, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    for name, _ in _SETTINGS_COLUMNS:
        op.drop_column("system_settings", name)

    op.drop_index("ix_worker_positions_field_worker_id_reported_at", table_name="worker_positions")
    op.drop_table("worker_positions")

    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id_read_at", table_name="notifications")
    op.drop_table("notifications")

    op.drop_table("alert_rules")

    op.drop_index("ix_alerts_route_id", table_name="alerts")
    op.drop_index("ix_alerts_status", table_name="alerts")
    op.drop_table("alerts")

    postgresql.ENUM(name="alert_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="alert_severity").drop(bind, checkfirst=True)
    _alert_kind.drop(bind, checkfirst=True)
