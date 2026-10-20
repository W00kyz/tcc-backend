"""automatic allocation: drop manual planning and reset operational data

Revision ID: f3b9d1a7c2e5
Revises: e7a2b4c6d8f0
Create Date: 2026-10-20 14:00:00

Spec docs/specs/2026-10-20-alocacao-automatica-design.md §5.3/§5.6 (owner: no existing data
needs to be preserved). Routes stop being planned by a manager: the template, assignment-chain
and manual-completion tables go, the route/stop/alert enums take their new value sets, and every
operational row is wiped. The registry (users, companies, types, buildings, floors, QR codes,
points, forms, settings, privacy) is left as it was.

The photos of the wiped evidence stay in MinIO — Alembic does not reach object storage. Run
`uv run python -m app.scripts.purge_orphan_evidence` after upgrading (backend/CONTRIBUTING.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3b9d1a7c2e5"
down_revision: str | Sequence[str] | None = "e7a2b4c6d8f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATIONAL_TABLES = (
    "routes",
    "route_stops",
    "executions",
    "qr_scans",
    "evidence_items",
    "answers",
    "manual_completions",
    "stop_assignments",
    "route_template_stops",
    "route_templates",
    "impediments",
    "alerts",
    "notifications",
    "worker_positions",
)
_DROPPED_TABLES = (
    "manual_completions",
    "stop_assignments",
    "route_template_stops",
    "route_templates",
)
_DROPPED_ENUMS = ("route_type", "template_recurrence", "stop_assignment_outcome")

# enum name -> (new values, the (table, column) pairs typed by it)
_RECREATED_ENUMS: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    "route_status": (("IN_PROGRESS", "PAUSED", "DONE"), (("routes", "status"),)),
    "route_stop_status": (
        ("PENDING", "IN_PROGRESS", "DONE", "RELEASED"),
        (("route_stops", "status"),),
    ),
    "execution_source": (("APP", "APPEAL"), (("executions", "source"),)),
    "alert_kind": (
        ("NO_EVIDENCE", "POINT_IMPEDED", "BATCH_RELEASED", "POINTS_UNSERVED", "APPEAL_FILED"),
        (("alerts", "kind"), ("alert_rules", "rule_kind")),
    ),
}

_RELEASE_REASON = postgresql.ENUM(
    "IMPEDIMENT",
    "ROUTE_REMAINDER",
    "LEFT_CAMPUS",
    "LOCATION_SILENCE",
    "END_OF_DAY",
    name="release_reason",
    create_type=False,
)

# Spec §5.5 initial values; the campus rectangle is the UFCG bbox of infra/osm/bbox.env.
_NEW_SETTINGS: tuple[tuple[str, sa.types.TypeEngine[object], str], ...] = (
    ("allocation_batch_size", sa.Integer(), "4"),
    ("campus_min_lat", sa.Float(), "-7.2250"),
    ("campus_max_lat", sa.Float(), "-7.2050"),
    ("campus_min_lng", sa.Float(), "-35.9200"),
    ("campus_max_lng", sa.Float(), "-35.8980"),
    ("outside_campus_release_minutes", sa.Integer(), "10"),
    ("location_silence_warning_minutes", sa.Integer(), "30"),
    ("location_silence_release_minutes", sa.Integer(), "45"),
    ("workday_end_time", sa.Time(), "17:00"),
    ("service_weekdays", postgresql.ARRAY(sa.Integer()), "{0,1,2,3,4}"),
    ("appeal_window_hours", sa.Integer(), "48"),
)
_DROPPED_SETTINGS = ("late_start_tolerance_min", "absence_tolerance_min", "late_stop_tolerance_min")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(f"TRUNCATE {', '.join(_OPERATIONAL_TABLES)} CASCADE")
    # Route columns first: routes.template_id still holds a foreign key to route_templates.
    _reset_route_columns()
    for table in _DROPPED_TABLES:
        op.drop_table(table)
    # alert_rules is registry-like (one row per kind), but its kinds are changing: the old rows
    # go before the enum swap (their values would not cast) and the new ones come after it.
    op.execute("DELETE FROM alert_rules")
    for name, (values, columns) in _RECREATED_ENUMS.items():
        _recreate_enum(name, values, columns)
    for name in _DROPPED_ENUMS:
        op.execute(f"DROP TYPE {name}")
    _create_exclusivity_guards()
    op.alter_column("alerts", "route_id", nullable=True)
    _seed_alert_rules()
    _reset_settings_columns()


def _reset_route_columns() -> None:
    for column in ("route_type", "template_id", "scheduled_start_at", "cancellation_reason"):
        op.drop_column("routes", column)
    op.add_column("routes", sa.Column("last_allocated_at", sa.DateTime(timezone=True)))

    op.drop_column("route_stops", "expected_arrival_from")
    op.drop_column("route_stops", "expected_arrival_to")
    _RELEASE_REASON.create(op.get_bind())
    # No server default needed for the NOT NULL columns: the table was just truncated.
    op.add_column("route_stops", sa.Column("batch_number", sa.Integer(), nullable=False))
    op.add_column("route_stops", sa.Column("service_date", sa.Date(), nullable=False))
    op.add_column("route_stops", sa.Column("released_at", sa.DateTime(timezone=True)))
    op.add_column("route_stops", sa.Column("release_reason", _RELEASE_REASON))


def _create_exclusivity_guards() -> None:
    # One route per worker per day (spec §5.3): a second "Iniciar rota" reopens the same route.
    op.create_unique_constraint(
        "uq_routes_field_worker_id_route_date", "routes", ["field_worker_id", "route_date"]
    )
    # A point is live (pending, in progress or done) on at most one stop per day. A RELEASED
    # stop drops out of the index, so the point can be claimed again. Created after the enum
    # swap: the predicate compares against the new route_stop_status type.
    op.create_index(
        "uq_route_stops_live_point_day",
        "route_stops",
        ["service_point_id", "service_date"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'IN_PROGRESS', 'DONE')"),
    )


def _recreate_enum(
    name: str, values: tuple[str, ...], columns: tuple[tuple[str, str], ...]
) -> None:
    """Postgres cannot drop a value from an enum: rename the old type, create the new one and
    retype the columns. The tables are empty, so the text cast never meets an old value."""
    op.execute(f"ALTER TYPE {name} RENAME TO {name}_old")
    postgresql.ENUM(*values, name=name).create(op.get_bind())
    for table, column in columns:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {name} USING {column}::text::{name}"
        )
    op.execute(f"DROP TYPE {name}_old")


def _seed_alert_rules() -> None:
    # One enabled row per new kind, NULL threshold (falls back to the global default), as the
    # Etapa 9 seed did.
    kinds = _RECREATED_ENUMS["alert_kind"][0]
    op.execute(
        "INSERT INTO alert_rules (rule_kind, enabled, threshold_minutes) VALUES "
        + ", ".join(f"('{kind}', true, NULL)" for kind in kinds)
    )


def _reset_settings_columns() -> None:
    for column in _DROPPED_SETTINGS:
        op.drop_column("system_settings", column)
    for column, column_type, default in _NEW_SETTINGS:
        op.add_column(
            "system_settings",
            sa.Column(column, column_type, nullable=False, server_default=sa.text(f"'{default}'")),
        )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("destructive reset; restore from backup")
