"""etapa 5 execution evidence

Revision ID: 2c8491d45546
Revises: 602731d8a203
Create Date: 2026-09-01 20:03:40.636213

Hand-written (project rule — no autogenerate). Ordering that matters: the three new enum
types are created before any column uses them; `route_stop_status` gains `IN_PROGRESS` via
ALTER TYPE ADD VALUE (PostgreSQL keeps this transaction-safe as long as the value is not
*used* in the same transaction — this migration only adds it, Task 4 starts writing it).
The Etapa 2 placeholder key/value `system_settings` table (revision 260828155124) had no
consumer and is replaced here by the typed RF32 singleton the radius check reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2c8491d45546"
down_revision: str | Sequence[str] | None = "602731d8a203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: each type is created (and dropped) explicitly below. A bare op.add_column
# or op.create_table with an Enum would otherwise emit CREATE TYPE a second time and fail on
# the duplicate — the same trap the Etapa 4 migration (602731d8a203) documents.
_qr_scan_kind = postgresql.ENUM("CHECK_IN", "CHECK_OUT", name="qr_scan_kind", create_type=False)
_review_status = postgresql.ENUM(
    "NONE", "PENDING_REVIEW", "RESOLVED", name="execution_review_status", create_type=False
)
_evidence_kind = postgresql.ENUM("PHOTO", "NOTE", name="evidence_kind", create_type=False)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # 1. new enum values / types
    op.execute("ALTER TYPE route_stop_status ADD VALUE IF NOT EXISTS 'IN_PROGRESS' BEFORE 'DONE'")
    _qr_scan_kind.create(bind, checkfirst=True)
    _review_status.create(bind, checkfirst=True)
    _evidence_kind.create(bind, checkfirst=True)

    # 2. qr_scans: new columns, and latitude/longitude relax to nullable (Task 4 stores NULL
    # when the device has no GPS fix).
    op.add_column(
        "qr_scans",
        sa.Column("kind", _qr_scan_kind, nullable=False, server_default="CHECK_IN"),
    )
    op.add_column(
        "qr_scans",
        sa.Column(
            "service_point_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_points.id"),
            nullable=True,
        ),
    )
    op.alter_column("qr_scans", "kind", server_default=None)
    op.alter_column("qr_scans", "latitude", existing_type=sa.Float(), nullable=True)
    op.alter_column("qr_scans", "longitude", existing_type=sa.Float(), nullable=True)

    # 3. executions
    op.add_column(
        "executions",
        sa.Column("checkout_idempotency_key", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_unique_constraint(
        "executions_checkout_idempotency_key_key", "executions", ["checkout_idempotency_key"]
    )
    op.add_column(
        "executions",
        sa.Column("review_status", _review_status, nullable=False, server_default="NONE"),
    )
    op.add_column(
        "executions",
        sa.Column("validation_flags", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.alter_column("executions", "review_status", server_default=None)
    op.alter_column("executions", "validation_flags", server_default=None)

    # 4. evidence_items
    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("executions.id"),
            nullable=False,
        ),
        sa.Column("kind", _evidence_kind, nullable=False),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "(kind = 'PHOTO' AND object_key IS NOT NULL AND content_type IS NOT NULL "
            "AND byte_size IS NOT NULL AND sha256 IS NOT NULL AND text_body IS NULL) "
            "OR (kind = 'NOTE' AND text_body IS NOT NULL AND object_key IS NULL)",
            name="evidence_items_kind_shape",
        ),
    )
    op.create_index("ix_evidence_items_execution_id", "evidence_items", ["execution_id"])

    # 5. manual_completions
    op.create_table(
        "manual_completions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "route_stop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("route_stops.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("executions.id"),
            nullable=False,
        ),
        sa.Column(
            "completed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
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
    # route_stop_id needs no explicit index: its UNIQUE constraint (one manual completion per
    # stop, spec §3.6) is already index-backed by Postgres.

    # 6. system_settings singleton — replaces the Etapa 2 key/value placeholder (no consumer).
    op.drop_table("system_settings")
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.text("true")),
        sa.Column("check_radius_meters", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id", name="system_settings_singleton"),
    )
    op.execute("INSERT INTO system_settings (id, check_radius_meters) VALUES (true, 50)")

    # 7. indexes folded from the Etapa 4 review
    op.create_index("ix_routes_route_date", "routes", ["route_date"])
    op.create_index("ix_executions_field_worker_id", "executions", ["field_worker_id"])
    op.create_index("ix_executions_route_stop_id", "executions", ["route_stop_id"])
    op.create_index("ix_executions_checked_in_at", "executions", ["checked_in_at"])
    op.create_index("ix_qr_scans_execution_id", "qr_scans", ["execution_id"])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    op.drop_index("ix_qr_scans_execution_id", table_name="qr_scans")
    op.drop_index("ix_executions_checked_in_at", table_name="executions")
    op.drop_index("ix_executions_route_stop_id", table_name="executions")
    op.drop_index("ix_executions_field_worker_id", table_name="executions")
    op.drop_index("ix_routes_route_date", table_name="routes")

    # system_settings: back to the Etapa 2 key/value placeholder shape.
    op.drop_table("system_settings")
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("key"),
    )

    op.drop_table("manual_completions")

    op.drop_index("ix_evidence_items_execution_id", table_name="evidence_items")
    op.drop_table("evidence_items")

    op.drop_constraint("executions_checkout_idempotency_key_key", "executions", type_="unique")
    op.drop_column("executions", "validation_flags")
    op.drop_column("executions", "review_status")
    op.drop_column("executions", "checkout_idempotency_key")

    op.alter_column("qr_scans", "longitude", existing_type=sa.Float(), nullable=False)
    op.alter_column("qr_scans", "latitude", existing_type=sa.Float(), nullable=False)
    op.drop_column("qr_scans", "service_point_id")
    op.drop_column("qr_scans", "kind")

    _evidence_kind.drop(bind, checkfirst=True)
    _review_status.drop(bind, checkfirst=True)
    _qr_scan_kind.drop(bind, checkfirst=True)

    # route_stop_status keeps the IN_PROGRESS value: PostgreSQL has no DROP VALUE for an
    # enum type (same limitation the Etapa 4 migration notes for its own enums). Harmless —
    # an unused label costs nothing and a fresh DB rebuilds the type from scratch.
