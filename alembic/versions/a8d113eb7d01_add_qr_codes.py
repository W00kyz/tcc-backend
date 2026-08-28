"""add qr_codes

Revision ID: a8d113eb7d01
Revises: ffe49b6c6217
Create Date: 2026-08-28 16:42:16.907437

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8d113eb7d01"
down_revision: str | Sequence[str] | None = "ffe49b6c6217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "qr_codes",
        sa.Column("floor_id", sa.UUID(), nullable=False),
        sa.Column("public_code", sa.String(length=500), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "REVOKED", "REPLACED", name="qr_code_status"),
            nullable=False,
        ),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.Column("revocation_reason", sa.String(length=300), nullable=True),
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
        sa.ForeignKeyConstraint(["floor_id"], ["floors.id"]),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["qr_codes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_code"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("qr_codes")
