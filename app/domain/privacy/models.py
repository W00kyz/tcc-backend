"""LGPD (RNF10): versioned privacy notices and per-user consent records. A notice is
published by a migration (spec §2 Ruling 2) — this module owns no write rule about *when*
a new version appears, only the shape."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PrivacyNotice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "privacy_notices"
    # Partial unique index: at most one row with is_current = true. Mirrored verbatim by the
    # raw `op.execute(...)` in migration 3cf7fec14642 (Alembic renders partial indexes fine,
    # but the raw SQL there is the source of truth for the WHERE clause).
    __table_args__ = (
        Index(
            "ix_privacy_notices_one_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    version: Mapped[int] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    body_markdown: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)


class ConsentRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        UniqueConstraint("user_id", "privacy_notice_id", name="uq_consent_user_notice"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    privacy_notice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("privacy_notices.id")
    )
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
