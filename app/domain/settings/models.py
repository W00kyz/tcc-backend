"""RF32 — configurable tolerance radius. One row, singleton via a boolean PK (Postgres trick).

Etapa 2 created a placeholder key/value `system_settings` table with no consumer; Etapa 5
(this migration) replaces it with the typed singleton the radius check actually reads."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"
    __table_args__ = (CheckConstraint("id", name="system_settings_singleton"),)

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    check_radius_meters: Mapped[int] = mapped_column(Integer, default=50)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
