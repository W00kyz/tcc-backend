"""drop dynamic forms

Revision ID: a9d2c4e6f1b3
Revises: 643c2dd3ca42
Create Date: 2026-10-26 09:00:00

Hand-written (project rule — no autogenerate). The dynamic form feature is gone (spec
docs/specs/2026-10-26-exige-evidencias-design.md §6): the four form tables, the nullable
`executions.form_version_id` column (it never had a foreign key) and the two Postgres enum
types they used. What the forms held is dropped with them; the evidence items stay.

The downgrade rebuilds the schema as `7f3a1b9c2d84` plus `d4f1c2a9b8e7` left it — empty, since
the dropped rows cannot be recovered.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a9d2c4e6f1b3"
down_revision: str | Sequence[str] | None = "643c2dd3ca42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the downgrade creates each type explicitly (same as 7f3a1b9c2d84), a bare
# create_table with an Enum would emit CREATE TYPE a second time.
_form_version_status = postgresql.ENUM(
    "DRAFT", "PUBLISHED", name="form_version_status", create_type=False
)
_question_type = postgresql.ENUM(
    "TEXT",
    "NUMBER",
    "BOOLEAN",
    "SINGLE_CHOICE",
    "MULTI_CHOICE",
    "MEDIA",
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

    # FK order: answers -> form_questions -> form_versions -> forms.
    op.drop_table("answers")
    op.drop_table("form_questions")
    op.drop_index("uq_form_versions_one_draft", table_name="form_versions")
    op.drop_table("form_versions")
    op.drop_table("forms")
    op.drop_column("executions", "form_version_id")

    _question_type.drop(bind, checkfirst=True)
    _form_version_status.drop(bind, checkfirst=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    _form_version_status.create(bind, checkfirst=True)
    _question_type.create(bind, checkfirst=True)

    op.add_column(
        "executions",
        sa.Column("form_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

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
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "form_version_id", "stable_key", name="uq_form_questions_version_stable_key"
        ),
    )

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
