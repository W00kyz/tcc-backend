"""Impediment reports that trigger emergency substitution/reassignment (RF16-RF21, Etapa 8).

An `Impediment` is only the field worker's request — "I cannot execute this stop/the rest of
this route" — reported from the app with the device-local `reported_at` and reconciled against
server time at `received_at`. It never touches `stop_assignments` itself: the manager reviews
the impediment and, if a reassignment is warranted, the routing module appends a new
`StopAssignment` row with `sequence + 1` (spec §3 Ruling 2 / `app/domain/routing/models.py`
module docstring). Keeping the request and the append-only assignment chain in separate tables
is what lets a rejected or duplicate impediment leave no mark on that audit chain."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.catalog.models import FieldWorker
    from app.domain.routing.models import Route, RouteStop


class ImpedimentScope(enum.StrEnum):
    # STOP: the worker cannot execute this one stop. ROUTE_REMAINDER: nothing left on the
    # route today (e.g. a vehicle breakdown) — route_stop_id is NULL in that case.
    STOP = "STOP"
    ROUTE_REMAINDER = "ROUTE_REMAINDER"


class ImpedimentReason(enum.StrEnum):
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    SITE_REFUSAL = "SITE_REFUSAL"
    HEALTH_ISSUE = "HEALTH_ISSUE"
    OTHER = "OTHER"


class ImpedimentStatus(enum.StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"


class Impediment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "impediments"

    route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routes.id"), index=True
    )
    # Nullable: NULL when scope is ROUTE_REMAINDER — the impediment covers the rest of the
    # route, not one specific stop.
    route_stop_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id"), nullable=True
    )
    scope: Mapped[ImpedimentScope] = mapped_column(Enum(ImpedimentScope, name="impediment_scope"))
    reason: Mapped[ImpedimentReason] = mapped_column(
        Enum(ImpedimentReason, name="impediment_reason")
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    # Device-local clock (Etapa 6 offline-first): when the worker actually reported it,
    # possibly before the request reaches the server.
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Server clock: when the request was received — the field the manager's queue orders by.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[ImpedimentStatus] = mapped_column(
        Enum(ImpedimentStatus, name="impediment_status"), default=ImpedimentStatus.PENDING
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Read-only lookups — no call site today needs the inverse direction (Route/RouteStop/
    # FieldWorker have no `impediments` collection), same pattern as RouteStop.service_type.
    route: Mapped["Route"] = relationship()
    route_stop: Mapped["RouteStop | None"] = relationship()
    reported_by_worker: Mapped["FieldWorker"] = relationship()
