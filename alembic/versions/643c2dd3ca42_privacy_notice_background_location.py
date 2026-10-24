"""privacy notice covers background location

Revision ID: 643c2dd3ca42
Revises: c6e1f8a2b4d9
Create Date: 2026-10-24 10:00:00

Hand-written (project rule — no autogenerate). Data only. Spec docs/specs/2026-10-20-alocacao-
automatica-design.md §7.3: with the worker's permission the app now also collects location with
the app in the background during a route, so the privacy notice must say so. The notice is data
(Etapa 11, revision 3cf7fec14642): this publishes a new version — the current body plus one
sentence — and makes it the current one, which is what makes the consent gate ask every user to
accept again.

The new body is built from the current row, not from a copy here: the author replaces the v1
placeholder with the real text before the pilot, and that text must survive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "643c2dd3ca42"
down_revision: str | Sequence[str] | None = "c6e1f8a2b4d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SENTENCE = (
    "Durante a rota, se você permitir, a localização também é coletada com o app em segundo plano."
)
_SUFFIX = f"\n\n{_SENTENCE}"


def upgrade() -> None:
    """Upgrade schema."""
    # Inserted as not current first: the partial unique index admits one current row at a time,
    # and is checked row by row.
    op.execute(
        sa.text(
            "INSERT INTO privacy_notices (id, version, title, body_markdown, "
            "published_at, is_current, created_at, updated_at) "
            "SELECT gen_random_uuid(), version + 1, title, body_markdown || :suffix, "
            "now(), false, now(), now() FROM privacy_notices WHERE is_current"
        ).bindparams(suffix=_SUFFIX)
    )
    _make_latest_current()


def downgrade() -> None:
    """Downgrade schema."""
    # Exactly the row upgrade() inserted: the latest version, whose body is its predecessor's
    # body plus the sentence — never another notice that happens to mention it.
    added = (
        "SELECT n.id FROM privacy_notices n "
        "JOIN privacy_notices p ON p.version = n.version - 1 "
        "WHERE n.version = (SELECT max(version) FROM privacy_notices) "
        "AND n.body_markdown = p.body_markdown || :suffix"
    )
    # The FK from consent_records would block the delete: acceptances of the removed version go.
    op.execute(
        sa.text(f"DELETE FROM consent_records WHERE privacy_notice_id IN ({added})").bindparams(
            suffix=_SUFFIX
        )
    )
    op.execute(
        sa.text(f"DELETE FROM privacy_notices WHERE id IN ({added})").bindparams(suffix=_SUFFIX)
    )
    _make_latest_current()


def _make_latest_current() -> None:
    op.execute("UPDATE privacy_notices SET is_current = false WHERE is_current")
    op.execute(
        "UPDATE privacy_notices SET is_current = true "
        "WHERE version = (SELECT max(version) FROM privacy_notices)"
    )
