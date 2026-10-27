"""Routes and their stops (spec 2026-10-20-alocacao-automatica §5.3). A route is one worker's
day; its stops are handed out in batches by the automatic allocation and given back to the pool
(RELEASED) when the worker cannot go on. Nobody plans or reassigns a route by hand."""

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    # Avoids a real circular import at runtime — catalog does not import routing, but
    # mypy strict still wants the annotation resolvable without executing the import eagerly.
    from app.domain.catalog.models import FieldWorker, ServicePoint, ServiceType


class RouteStatus(enum.StrEnum):
    # Spec 2026-10-20-alocacao-automatica §5.3. A route is created already IN_PROGRESS by the
    # worker's first "Iniciar rota" of the day. PAUSED: its live stops were released (rest-of-
    # route impediment, left campus, location silence); "Iniciar rota" again reopens it with a
    # new batch. DONE: the pool of the worker's types ran out, or the workday ended.
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    DONE = "DONE"


class RouteStopStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    # Given back to the pool (spec §5.3): `released_at` and `release_reason` say when and why.
    RELEASED = "RELEASED"


class ReleaseReason(enum.StrEnum):
    IMPEDIMENT = "IMPEDIMENT"
    ROUTE_REMAINDER = "ROUTE_REMAINDER"
    LEFT_CAMPUS = "LEFT_CAMPUS"
    LOCATION_SILENCE = "LOCATION_SILENCE"
    END_OF_DAY = "END_OF_DAY"


# create_type=False: the Postgres enums are hand-created by the migrations (Etapa 4
# 602731d8a203, recreated by the allocation reset f3b9d1a7c2e5).
ROUTE_STATUS_ENUM = Enum(RouteStatus, name="route_status", create_type=False)
ROUTE_STOP_STATUS_ENUM = Enum(RouteStopStatus, name="route_stop_status", create_type=False)
RELEASE_REASON_ENUM = Enum(ReleaseReason, name="release_reason", create_type=False)


class Route(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "routes"
    # One route per worker per day (spec §5.3): a second "Iniciar rota" reopens the same route.
    __table_args__ = (
        UniqueConstraint(
            "field_worker_id", "route_date", name="uq_routes_field_worker_id_route_date"
        ),
    )

    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    # The campus day the route belongs to — the key the app's "rota do dia" (RF27) and the
    # panel board filter by.
    route_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[RouteStatus] = mapped_column(ROUTE_STATUS_ENUM, default=RouteStatus.IN_PROGRESS)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    start_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When the latest batch was handed out; the location-silence clock starts here when the
    # worker never sent a position.
    last_allocated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route", order_by="RouteStop.order_index"
    )
    # No back_populates: FieldWorker has no `routes` collection yet, and none of today's
    # call sites need one — this is a read-only lookup for RouteOut.field_worker_name.
    field_worker: Mapped["FieldWorker"] = relationship()


class RouteStop(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_stops"
    # The exclusivity guarantee (spec §5.3): a point is live on at most one stop per day. A
    # released point gets a new stop on another route; its day history is the sequence of stops.
    __table_args__ = (
        Index(
            "uq_route_stops_live_point_day",
            "service_point_id",
            "service_date",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'IN_PROGRESS', 'DONE')"),
        ),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    service_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_points.id")
    )
    order_index: Mapped[int] = mapped_column(Integer)
    # Which allocation batch of the route handed this stop out (1, 2, ...).
    batch_number: Mapped[int] = mapped_column(Integer)
    # A copy of `route.route_date`, so the exclusivity index above needs no join.
    service_date: Mapped[date] = mapped_column(Date)
    # Which service type is executed at this stop. Nullable: a stop may have no type.
    service_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), nullable=True
    )
    status: Mapped[RouteStopStatus] = mapped_column(
        ROUTE_STOP_STATUS_ENUM, default=RouteStopStatus.PENDING
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[ReleaseReason | None] = mapped_column(RELEASE_REASON_ENUM, nullable=True)
    # The "leg" is always from the previous stop to this one in the current order (spec §3.2):
    # no separate route_legs table. NULL on the first stop and in OSRM-degraded mode (Ruling 6).
    distance_from_prev_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_from_prev_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # [[lng, lat], ...] GeoJSON coordinate order — the polyline the app draws (RF28).
    leg_geometry: Mapped[list[list[float]] | None] = mapped_column(JSONB, nullable=True)

    route: Mapped["Route"] = relationship(back_populates="stops")
    service_point: Mapped["ServicePoint"] = relationship()
    # Read-only lookup for RouteStopOut.service_type_name. Nullable — a stop with no
    # service_type_id has no type. One-directional.
    service_type: Mapped["ServiceType | None"] = relationship()
