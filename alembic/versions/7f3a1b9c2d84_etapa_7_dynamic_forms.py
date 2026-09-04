"""etapa 7 dynamic forms

Revision ID: 7f3a1b9c2d84
Revises: 4e1d2c3b5a6f
Create Date: 2026-09-04 09:00:00.000000

Hand-written (project rule — no autogenerate). Creates the four Etapa 7 tables (`forms`,
`form_versions`, `form_questions`, `answers`), the two new enum types, and the nullable
`route_stops.service_type_id` column (spec §3.4 — no backfill). `executions.form_version_id`
already exists (nullable, since Etapa 5), so nothing to do there.

The partial unique index `uq_form_versions_one_draft` is what enforces "exactly one DRAFT per
form" (spec §3.1); a plain UNIQUE cannot express the WHERE clause.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7f3a1b9c2d84"
down_revision: str | Sequence[str] | None = "4e1d2c3b5a6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: each type is created (and dropped) explicitly below, mirroring the
# Etapa 5 migration (2c8491d45546) — a bare create_table with an Enum would otherwise emit
# CREATE TYPE a second time and fail on the duplicate.
_form_version_status = postgresql.ENUM(
    "DRAFT", "PUBLISHED", name="form_version_status", create_type=False
)
_question_type = postgresql.ENUM(
    "TEXT",
    "NUMBER",
    "BOOLEAN",
    "SINGLE_CHOICE",
    "MULTI_CHOICE",
    name="question_type",
    create_type=False,
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

    # 1. enum types
    _form_version_status.create(bind, checkfirst=True)
    _question_type.create(bind, checkfirst=True)

    # 2. forms — one per service type
    op.create_table(
        "forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "service_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_types.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        *_timestamp_columns(),
    )

    # 3. form_versions — one mutable DRAFT + immutable PUBLISHED versions
    op.create_table(
        "form_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "form_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("forms.id"),
            nullable=False,
        ),
        sa.Column("status", _form_version_status, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
    )
    op.create_index(
        "uq_form_versions_one_draft",
        "form_versions",
        ["form_id"],
        unique=True,
        postgresql_where=sa.text("status = 'DRAFT'"),
    )

    # 4. form_questions
    op.create_table(
        "form_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "form_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("form_versions.id"),
            nullable=False,
        ),
        sa.Column("stable_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.String(500), nullable=False),
        sa.Column("question_type", _question_type, nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "form_version_id", "stable_key", name="uq_form_questions_version_stable_key"
        ),
    )
    op.alter_column("form_questions", "options", server_default=None)

    # 5. answers — lives with execution; no FK to form_questions (stable_key is the link)
    op.create_table(
        "answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question_stable_key", sa.String(64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "execution_id", "question_stable_key", name="uq_answers_execution_stable_key"
        ),
    )

    # 6. route_stops.service_type_id — nullable, no backfill (spec §3.2)
    op.add_column(
        "route_stops",
        sa.Column(
            "service_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_types.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    op.drop_column("route_stops", "service_type_id")
    op.drop_table("answers")
    op.drop_table("form_questions")
    op.drop_index("uq_form_versions_one_draft", table_name="form_versions")
    op.drop_table("form_versions")
    op.drop_table("forms")

    _question_type.drop(bind, checkfirst=True)
    _form_version_status.drop(bind, checkfirst=True)
