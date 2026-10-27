"""Minimal catalog tables (spec §4.1). RF05-RF10's write paths and endpoints (Etapa 3) live in
app/api/*.py, one router per table here — this module stays models-only."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.identity.models import User


class ContractorCompany(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contractor_companies"

    name: Mapped[str] = mapped_column(String(200))
    cnpj: Mapped[str] = mapped_column(String(14), unique=True)
    # The service types this company performs (a worker's pool is the points of these types).
    service_types: Mapped[list["ServiceType"]] = relationship(
        back_populates="contractor_company", order_by="ServiceType.name"
    )


class ServiceType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """RNF19's load-bearing table: a row here, never a code branch, is what a 'service type' is.
    It belongs to a company; a type without one (migrated data) is outside every worker's pool
    until a manager adds it to a company."""

    __tablename__ = "service_types"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    average_duration_minutes: Mapped[int]
    contractor_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contractor_companies.id"), nullable=True
    )
    contractor_company: Mapped["ContractorCompany | None"] = relationship(
        back_populates="service_types"
    )
    # Whether finishing a stop of this type needs a photo or file as evidence (spec
    # 2026-10-26-exige-evidencias). Copied to each stop when it is allocated.
    requires_evidence: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class FieldWorkerStatus(enum.StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FieldWorker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "field_workers"

    full_name: Mapped[str] = mapped_column(String(200))
    contractor_company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contractor_companies.id")
    )
    contractor_company: Mapped["ContractorCompany"] = relationship()
    # Every worker has a login: they register themselves (POST /auth/register).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True
    )
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    status: Mapped[FieldWorkerStatus] = mapped_column(
        Enum(FieldWorkerStatus, name="field_worker_status"),
        default=FieldWorkerStatus.PENDING_APPROVAL,
    )
    # A manager can switch an approved worker off without losing their history.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Building(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "buildings"

    name: Mapped[str] = mapped_column(String(200))
    campus_area: Mapped[str] = mapped_column(String(200))
    # GeoJSON Polygon outline drawn on the dashboard map. JSONB, not PostGIS geometry: the
    # outline is only drawn, never queried spatially (spec 2026-10-11-predios-no-mapa).
    # Validated by app/domain/catalog/footprint.py before being stored.
    footprint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Where the outline came from ("osm" or "drawn" by hand on the dashboard) and, for "osm",
    # its way id, so it can be traced back to the source and reimported.
    footprint_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    osm_way_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Floor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One QR code per floor (spec decision #5 — the QR belongs to the floor, GPS disambiguates
    the room). building_id + label unique: two "Térreo" rows in the same building make no sense."""

    __tablename__ = "floors"
    __table_args__ = (UniqueConstraint("building_id", "label"),)

    building_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("buildings.id"))
    label: Mapped[str] = mapped_column(String(100))
    # Task 8: lets the QR sheet PDF endpoint read floor.building.name via selectinload,
    # without a second query.
    building: Mapped["Building"] = relationship()


class ServicePoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """latitude/longitude are plain floats here — see "Decisões de escopo desta etapa" item 3
    for why the PostGIS geography column waits for RF19/RF32."""

    __tablename__ = "service_points"

    floor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("floors.id"))
    # Read-only lookup for RouteStopOut (Etapa 4): floor label + building name via one
    # selectinload chain, no back_populates — no call site needs Floor.service_points.
    floor: Mapped["Floor"] = relationship()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    # Nullable in the database only because migrated points may have no type yet; the API
    # requires one, and a point without a type is outside every worker's pool.
    service_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), nullable=True
    )
    service_type: Mapped["ServiceType | None"] = relationship()
