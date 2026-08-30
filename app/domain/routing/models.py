"""Routes and assignment (spec §4.1, decision #2). StopAssignment is append-only: a
reassignment (Etapa 8) inserts a new row with sequence + 1, never updates one — that is what
makes RF21's audit chain a structural guarantee instead of a trigger."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    # Avoids a real circular import at runtime — catalog does not import routing, but
    # mypy strict still wants the annotation resolvable without executing the import eagerly.
    from app.domain.catalog.models import FieldWorker, ServicePoint


class Route(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "routes"

    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    # timezone=True (not the plan's bare mapped_column): the caller always passes a
    # timezone-aware datetime (datetime.now(UTC)) — asyncpg rejects binding that into a
    # bare TIMESTAMP WITHOUT TIME ZONE column, so this matches TimestampMixin's own choice.
    scheduled_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    start_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    stops: Mapped[list["RouteStop"]] = relationship(back_populates="route")
    # No back_populates: FieldWorker has no `routes` collection yet, and none of today's
    # call sites need one — this is a read-only lookup for RouteOut.field_worker_name.
    field_worker: Mapped["FieldWorker"] = relationship()


class RouteStopStatus(enum.StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"


class RouteStop(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_stops"

    route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    service_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_points.id")
    )
    order_index: Mapped[int] = mapped_column(Integer)
    expected_arrival_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expected_arrival_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[RouteStopStatus] = mapped_column(
        Enum(RouteStopStatus, name="route_stop_status"), default=RouteStopStatus.PENDING
    )

    route: Mapped["Route"] = relationship(back_populates="stops")
    service_point: Mapped["ServicePoint"] = relationship()


class StopAssignmentOutcome(enum.StrEnum):
    EXECUTED = "EXECUTED"
    IMPEDED = "IMPEDED"
    REASSIGNED = "REASSIGNED"
    CANCELLED = "CANCELLED"


class StopAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only. sequence=1 is always the originally designated worker (spec decision #2);
    a later reassignment (Etapa 8) inserts sequence=2, never edits sequence=1."""

    __tablename__ = "stop_assignments"

    route_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id")
    )
    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    assigned_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    transfer_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    outcome: Mapped[StopAssignmentOutcome | None] = mapped_column(
        Enum(StopAssignmentOutcome, name="stop_assignment_outcome"), nullable=True
    )
