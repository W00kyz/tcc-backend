"""RF32 — configurable tolerance radius. One row, singleton via a boolean PK (Postgres trick).

Etapa 2 created a placeholder key/value `system_settings` table with no consumer; Etapa 5
(this migration) replaces it with the typed singleton the radius check actually reads."""

import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, Time
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _default_service_weekdays() -> list[int]:
    return [0, 1, 2, 3, 4]


class SystemSettings(Base):
    __tablename__ = "system_settings"
    __table_args__ = (CheckConstraint("id", name="system_settings_singleton"),)

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    check_radius_meters: Mapped[int] = mapped_column(Integer, default=50)
    # Etapa 9 (RF50 / spec §2 Ruling 8): global defaults for the periodic alert scan and the
    # app's GPS report cadence. `alert_rules.threshold_minutes` overrides the per-rule ones.
    alert_scan_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    no_evidence_grace_min: Mapped[int] = mapped_column(Integer, default=30)
    position_report_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    # Etapa 11 (RNF10): retention windows for personal data. 0 = keep forever.
    worker_position_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    evidence_retention_days: Mapped[int] = mapped_column(Integer, default=365)
    # Automatic allocation and release (spec 2026-10-20-alocacao-automatica §5.5).
    allocation_batch_size: Mapped[int] = mapped_column(Integer, default=4)
    # The campus as a rectangle — enough for "is the worker at UFCG?" (infra/osm/bbox.env).
    campus_min_lat: Mapped[float] = mapped_column(Float, default=-7.2250)
    campus_max_lat: Mapped[float] = mapped_column(Float, default=-7.2050)
    campus_min_lng: Mapped[float] = mapped_column(Float, default=-35.9200)
    campus_max_lng: Mapped[float] = mapped_column(Float, default=-35.8980)
    outside_campus_release_minutes: Mapped[int] = mapped_column(Integer, default=10)
    location_silence_warning_minutes: Mapped[int] = mapped_column(Integer, default=30)
    location_silence_release_minutes: Mapped[int] = mapped_column(Integer, default=45)
    # Campus local time (America/Fortaleza, app/core/campus_clock.py), not UTC.
    workday_end_time: Mapped[time] = mapped_column(Time, default=time(17, 0))
    # `date.weekday()` numbers, Monday=0. No holiday calendar (owner: weekdays only).
    service_weekdays: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=_default_service_weekdays
    )
    appeal_window_hours: Mapped[int] = mapped_column(Integer, default=48)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
