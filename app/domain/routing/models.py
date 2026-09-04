"""Routes and assignment (spec §4.1, decision #2). StopAssignment is append-only: a
reassignment (Etapa 8) inserts a new row with sequence + 1, never updates one — that is what
makes RF21's audit chain a structural guarantee instead of a trigger."""

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    # Avoids a real circular import at runtime — catalog does not import routing, but
    # mypy strict still wants the annotation resolvable without executing the import eagerly.
    from app.domain.catalog.models import FieldWorker, ServicePoint, ServiceType


class RouteType(enum.StrEnum):
    # Same value pair as catalog.PointType, but a SEPARATE Postgres enum (name="route_type"):
    # a route's regular/occasional flag (RF23/RF24) is not the same domain as a point's.
    REGULAR = "REGULAR"
    OCCASIONAL = "OCCASIONAL"


class RouteStatus(enum.StrEnum):
    # Explicit state machine (spec §3 Ruling 4): PLANNED -> IN_PROGRESS -> DONE, and
    # -> CANCELLED from PLANNED or IN_PROGRESS. `started_at` alone cannot tell a route
    # cancelled-before-start apart from one merely planned.
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    CANCELLED = "CANCELLED"
    DONE = "DONE"


# create_type=False: the Postgres enums are hand-created by the Etapa 4 migration
# (602731d8a203). One shared column type instead of a fresh Enum() per mapped_column keeps
# the `route_type` type — used by both Route and RouteTemplate — declared in exactly one place.
ROUTE_TYPE_ENUM = Enum(RouteType, name="route_type", create_type=False)
ROUTE_STATUS_ENUM = Enum(RouteStatus, name="route_status", create_type=False)


class Route(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "routes"

    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    # The operational date of the route (RF11 "para uma data específica") — the key the
    # app's "rota do dia" (RF27) and the panel board filter by. NOT NULL, no server default:
    # the migration backfills existing rows before applying the constraint.
    route_date: Mapped[date] = mapped_column(Date, index=True)
    route_type: Mapped[RouteType] = mapped_column(ROUTE_TYPE_ENUM, default=RouteType.REGULAR)
    status: Mapped[RouteStatus] = mapped_column(ROUTE_STATUS_ENUM, default=RouteStatus.PLANNED)
    cancellation_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Provenance: which template materialised this route (RF15). NULL for a hand-built route.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_templates.id"), nullable=True
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

    stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route", order_by="RouteStop.order_index"
    )
    # No back_populates: FieldWorker has no `routes` collection yet, and none of today's
    # call sites need one — this is a read-only lookup for RouteOut.field_worker_name.
    field_worker: Mapped["FieldWorker"] = relationship()


class RouteStopStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class RouteStop(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_stops"

    route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    service_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_points.id")
    )
    order_index: Mapped[int] = mapped_column(Integer)
    # Etapa 7: which service type is executed at this stop — drives the dynamic check-out
    # form. Nullable, no backfill (spec §3.2): an existing stop with no type has no form.
    service_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), nullable=True
    )
    expected_arrival_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expected_arrival_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[RouteStopStatus] = mapped_column(
        Enum(RouteStopStatus, name="route_stop_status"), default=RouteStopStatus.PENDING
    )
    # The "leg" is always from the previous stop to this one in the current order (spec §3.2):
    # no separate route_legs table. NULL on the first stop and in OSRM-degraded mode (Ruling 6).
    distance_from_prev_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_from_prev_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # [[lng, lat], ...] GeoJSON coordinate order — the polyline the app draws (RF28).
    leg_geometry: Mapped[list[list[float]] | None] = mapped_column(JSONB, nullable=True)

    route: Mapped["Route"] = relationship(back_populates="stops")
    service_point: Mapped["ServicePoint"] = relationship()
    # Etapa 7: read-only lookup for RouteStopOut.service_type_name and the embedded form.
    # Nullable — a stop with no service_type_id has no type and no form. One-directional.
    service_type: Mapped["ServiceType | None"] = relationship()
    # Ordered by sequence so `assignments[-1]` is always the current designation (spec §3.4):
    # reassignment appends sequence + 1, never edits an existing row. One-directional — no
    # StopAssignment.route_stop back-reference is needed by any call site today.
    assignments: Mapped[list["StopAssignment"]] = relationship(order_by="StopAssignment.sequence")


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
