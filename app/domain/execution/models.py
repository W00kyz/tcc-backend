"""Execution records (spec §4.1, decisions #3 and #7). checked_in_at is the device's own
clock (scanned_at from the client); synced_at is server "now" — two timestamps because the
offline mode (Etapa 6) must not lie about when the work actually happened."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExecutionSource(enum.StrEnum):
    APP = "APP"
    MANAGER_MANUAL = "MANAGER_MANUAL"


class Execution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "executions"

    route_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id")
    )
    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    # form_version_id arrives in Etapa 7; nullable until then — there is no form this stage.
    form_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # timezone=True (not the plan's bare mapped_column): the caller always passes a
    # timezone-aware datetime (datetime.now(UTC)) — asyncpg rejects binding that into a
    # bare TIMESTAMP WITHOUT TIME ZONE column, so this matches TimestampMixin's own choice.
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[ExecutionSource] = mapped_column(Enum(ExecutionSource, name="execution_source"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)


class GeoValidation(enum.StrEnum):
    VALIDATED = "VALIDATED"
    OUT_OF_RADIUS = "OUT_OF_RADIUS"
    NOT_VALIDATED = "NOT_VALIDATED"


class QrScan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "qr_scans"

    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("executions.id"))
    qr_code_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("qr_codes.id"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Always NOT_VALIDATED this stage — layers 2-5 (radius, route, time window, QR status)
    # arrive in Etapas 3 and 5. See "Decisões de escopo desta etapa", item 6.
    geo_validation: Mapped[GeoValidation] = mapped_column(
        Enum(GeoValidation, name="geo_validation")
    )
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
