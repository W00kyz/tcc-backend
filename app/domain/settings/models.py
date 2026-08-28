"""Global, admin-configurable parameters (spec §6): check-in radius, absence tolerance, alert
sweep frequency. Etapa 2 only creates the table; no endpoint reads or writes it yet — the first
real consumer is the Etapa 5 radius check."""

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
