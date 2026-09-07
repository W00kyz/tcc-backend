"""etapa 11 lgpd

Revision ID: 3cf7fec14642
Revises: 8433dd2af4fe
Create Date: 2026-09-07 16:57:03.730254

Hand-written (project rule — no autogenerate). LGPD groundwork (RNF10): the
`privacy_notices` / `consent_records` tables, a partial unique index that admits at most
one current notice, the two `system_settings` retention columns, and the seed of privacy
notice v1.

The partial unique index is emitted as raw SQL (`op.execute`) because its WHERE clause is
the source of truth; the ORM mirrors it with `Index(..., postgresql_where=text("is_current"))`.

The two `system_settings` columns land with a `server_default` (so the existing singleton
row gets a value) which is then dropped — the ORM's `mapped_column(default=...)` owns new
rows from here on (Etapa 9 pattern, revision 8433dd2af4fe).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3cf7fec14642"
down_revision: str | Sequence[str] | None = "8433dd2af4fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, server_default) for the two new system_settings retention columns.
_SETTINGS_COLUMNS = (
    ("worker_position_retention_days", "90"),
    ("evidence_retention_days", "365"),
)

_NOTICE_V1_BODY = (
    "[TEXTO DO AVISO DE PRIVACIDADE - v1. O autor substitui este placeholder pelo texto "
    "real antes do piloto: quais dados sao coletados (localizacao durante a rota, fotos de "
    "evidencia), para que, por quanto tempo sao retidos, e como consultar/contestar.]"
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
    op.create_table(
        "privacy_notices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamp_columns(),
    )
    op.alter_column("privacy_notices", "is_current", server_default=None)
    op.execute(
        "CREATE UNIQUE INDEX ix_privacy_notices_one_current "
        "ON privacy_notices (is_current) WHERE is_current"
    )

    op.create_table(
        "consent_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "privacy_notice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("privacy_notices.id"),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint("user_id", "privacy_notice_id", name="uq_consent_user_notice"),
    )

    for name, default in _SETTINGS_COLUMNS:
        op.add_column(
            "system_settings",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.alter_column("system_settings", name, server_default=None)

    op.execute(
        sa.text(
            "INSERT INTO privacy_notices (id, version, title, body_markdown, "
            "published_at, is_current, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 1, 'Aviso de Privacidade', :body, "
            "now(), true, now(), now())"
        ).bindparams(body=_NOTICE_V1_BODY)
    )


def downgrade() -> None:
    """Downgrade schema."""
    for name, _ in _SETTINGS_COLUMNS:
        op.drop_column("system_settings", name)

    op.drop_table("consent_records")
    op.execute("DROP INDEX IF EXISTS ix_privacy_notices_one_current")
    op.drop_table("privacy_notices")
