"""service types on the company, typed points, worker approval

Revision ID: a1c5e9b3d7f2
Revises: f3b9d1a7c2e5
Create Date: 2026-10-20 18:00:00

Spec docs/specs/2026-10-20-alocacao-automatica-design.md §5.1/§5.2/§5.6. A service type now
belongs to a company and a point has a type; the worker-to-type qualification table goes. Workers
register themselves and a manager approves them, so every worker has a login and a status.

Data rules (§5.6): with exactly one company, the existing types are adopted by it, otherwise they
stay without a company; with exactly one type, every point takes it, otherwise points stay
untyped; workers with a login become APPROVED, workers without one are deleted (the operational
rows that referenced them were wiped by the previous revision).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1c5e9b3d7f2"
down_revision: str | Sequence[str] | None = "f3b9d1a7c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_VALUES = ("PENDING_APPROVAL", "APPROVED", "REJECTED")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "service_types",
        sa.Column("contractor_company_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_types_contractor_company_id",
        "service_types",
        "contractor_companies",
        ["contractor_company_id"],
        ["id"],
    )
    op.execute(
        """
        UPDATE service_types
        SET contractor_company_id = (SELECT id FROM contractor_companies)
        WHERE (SELECT count(*) FROM contractor_companies) = 1
        """
    )

    op.add_column(
        "service_points",
        sa.Column("service_type_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_points_service_type_id",
        "service_points",
        "service_types",
        ["service_type_id"],
        ["id"],
    )
    op.execute(
        """
        UPDATE service_points
        SET service_type_id = (SELECT id FROM service_types)
        WHERE (SELECT count(*) FROM service_types) = 1
        """
    )

    op.drop_table("field_worker_service_types")

    op.execute("DELETE FROM field_workers WHERE user_id IS NULL")
    status_enum = postgresql.ENUM(*_STATUS_VALUES, name="field_worker_status")
    status_enum.create(op.get_bind())
    op.add_column(
        "field_workers",
        sa.Column(
            "status",
            postgresql.ENUM(*_STATUS_VALUES, name="field_worker_status", create_type=False),
            nullable=False,
            server_default="APPROVED",
        ),
    )
    op.alter_column("field_workers", "status", server_default=None)
    op.add_column(
        "field_workers",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "field_workers",
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "field_workers", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "field_workers", sa.Column("rejection_reason", sa.String(length=300), nullable=True)
    )
    op.create_foreign_key(
        "fk_field_workers_reviewed_by", "field_workers", "users", ["reviewed_by"], ["id"]
    )
    op.alter_column("field_workers", "user_id", nullable=False)
    op.create_unique_constraint("uq_field_workers_user_id", "field_workers", ["user_id"])


def downgrade() -> None:
    """Downgrade schema. Deleted workers and the type-to-company links are not restored."""
    op.drop_constraint("uq_field_workers_user_id", "field_workers", type_="unique")
    op.alter_column("field_workers", "user_id", nullable=True)
    op.drop_constraint("fk_field_workers_reviewed_by", "field_workers", type_="foreignkey")
    for column in ("rejection_reason", "reviewed_at", "reviewed_by", "is_active", "status"):
        op.drop_column("field_workers", column)
    postgresql.ENUM(name="field_worker_status").drop(op.get_bind())

    op.create_table(
        "field_worker_service_types",
        sa.Column("field_worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["field_worker_id"], ["field_workers.id"]),
        sa.ForeignKeyConstraint(["service_type_id"], ["service_types.id"]),
        sa.PrimaryKeyConstraint("field_worker_id", "service_type_id"),
    )

    op.drop_constraint("fk_service_points_service_type_id", "service_points", type_="foreignkey")
    op.drop_column("service_points", "service_type_id")
    op.drop_constraint(
        "fk_service_types_contractor_company_id", "service_types", type_="foreignkey"
    )
    op.drop_column("service_types", "contractor_company_id")
