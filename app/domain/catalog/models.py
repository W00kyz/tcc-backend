"""Minimal catalog tables (spec §4.1). No CRUD yet — RF05-RF10 build the write paths and
endpoints in Etapa 3. This module exists so Tasks 7-8 have a real foreign key to point at."""

import enum
import uuid
from datetime import date

from sqlalchemy import Enum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PointType(enum.StrEnum):
    REGULAR = "REGULAR"
    OCCASIONAL = "OCCASIONAL"


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
    # Task 8: lets the QR sheet PDF endpoint read floor.building.name via selectinload,
    # without a second query.
    building: Mapped["Building"] = relationship()


class ServicePoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """latitude/longitude are plain floats here — see "Decisões de escopo desta etapa" item 3
    for why the PostGIS geography column waits for RF19/RF32."""

    __tablename__ = "service_points"

    floor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("floors.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    point_type: Mapped[PointType] = mapped_column(
        Enum(PointType, name="point_type"), default=PointType.REGULAR
    )
    # Nullable: only an OCCASIONAL point references an event. Left in place after
    # promote_service_point_to_regular (RF26) as the point's historical origin — see that
    # function's docstring for the provenance rationale.
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=True
    )
    event: Mapped["Event | None"] = relationship()


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """RF22 — pontos eventuais se vinculam a um evento com janela de validade. Referenciado em
    docs/specs/2026-08-24-arquitetura-design.md §4.1 desde a Etapa 2, criado só agora (Etapa 3,
    Task 5) porque nenhuma tabela dependia dela até o ponto eventual existir."""

    __tablename__ = "events"

    name: Mapped[str] = mapped_column(String(200))
    valid_from: Mapped[date] = mapped_column()
    valid_until: Mapped[date] = mapped_column()
