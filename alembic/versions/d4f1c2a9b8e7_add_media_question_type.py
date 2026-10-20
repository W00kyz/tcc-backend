"""add MEDIA question type

Revision ID: d4f1c2a9b8e7
Revises: c3e8a5d27f10
Create Date: 2026-10-19 23:30:00

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "d4f1c2a9b8e7"
down_revision: str | Sequence[str] | None = "c3e8a5d27f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES_WITHOUT_MEDIA = "'TEXT', 'NUMBER', 'BOOLEAN', 'SINGLE_CHOICE', 'MULTI_CHOICE'"


def upgrade() -> None:
    """Upgrade schema."""
    # A photo question (docs/specs/2026-10-19-pergunta-midia-design.md). Postgres only lets a
    # new enum value be used after the transaction that added it commits: autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE question_type ADD VALUE IF NOT EXISTS 'MEDIA'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot drop an enum value: the type is rebuilt without it. A MEDIA question would
    # have no type to fall back to, so the downgrade refuses instead of silently retyping it.
    media_questions = op.get_bind().scalar(
        text("SELECT count(*) FROM form_questions WHERE question_type = 'MEDIA'")
    )
    if media_questions:
        raise RuntimeError(
            f"{media_questions} form question(s) are of type MEDIA; remove them before "
            "downgrading below d4f1c2a9b8e7."
        )
    op.execute("ALTER TYPE question_type RENAME TO question_type_with_media")
    op.execute(f"CREATE TYPE question_type AS ENUM ({_TYPES_WITHOUT_MEDIA})")
    op.execute(
        "ALTER TABLE form_questions ALTER COLUMN question_type TYPE question_type "
        "USING question_type::text::question_type"
    )
    op.execute("DROP TYPE question_type_with_media")
