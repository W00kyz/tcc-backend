"""Minimal catalog tables (spec §4.1). No CRUD yet — RF05-RF10 build the write paths and
endpoints in Etapa 3. This module exists so Tasks 7-8 have a real foreign key to point at."""

import uuid

from sqlalchemy import Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ContractorCompany(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contractor_companies"

    name: Mapped[str] = mapped_column(String(200))
    cnpj: Mapped[str] = mapped_column(String(14), unique=True)


class ServiceType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """RNF19's load-bearing table: a row here, never a code branch, is what a 'service type' is."""

    __tablename__ = "service_types"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    average_duration_minutes: Mapped[int]


class FieldWorker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "field_workers"

    full_name: Mapped[str] = mapped_column(String(200))
    contractor_company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contractor_companies.id")
    )
    # Links the login identity (Task 3) only when the field worker has app access —
    # nullable because a FieldWorker can be registered before being granted a User (RF08).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Task 3 (RF08): the join rows this FieldWorker qualifies for, eager-loaded via
    # selectinload so FieldWorkerOut.service_type_ids never triggers a lazy-load.
    service_type_links: Mapped[list["FieldWorkerServiceType"]] = relationship()


class FieldWorkerServiceType(Base):
    __tablename__ = "field_worker_service_types"

    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id"), primary_key=True
    )
    service_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), primary_key=True
    )


class Building(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "buildings"

    name: Mapped[str] = mapped_column(String(200))
    campus_area: Mapped[str] = mapped_column(String(200))


class Floor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One QR code per floor (spec decision #5 — the QR belongs to the floor, GPS disambiguates
    the room). building_id + label unique: two "Térreo" rows in the same building make no sense."""

    __tablename__ = "floors"
    __table_args__ = (UniqueConstraint("building_id", "label"),)

    building_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("buildings.id"))
    label: Mapped[str] = mapped_column(String(100))


class ServicePoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """latitude/longitude are plain floats here — see "Decisões de escopo desta etapa" item 3
    for why the PostGIS geography column waits for RF19/RF32."""

    __tablename__ = "service_points"

    floor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("floors.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
