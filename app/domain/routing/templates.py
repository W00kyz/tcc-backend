"""Recurring-route templates (RF15). A template speeds up route creation — it is materialised
manually into a Route by a manager (spec §5.2 Ruling 8); no job generates routes from it.
`weekdays` and `recurrence` are guidance for the manager and a UI filter, not a scheduler input.
"""

import enum
import uuid
from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Time
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.routing.models import RouteType

if TYPE_CHECKING:
    from app.domain.catalog.models import ServicePoint


class TemplateRecurrence(enum.StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class RouteTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_templates"

    name: Mapped[str] = mapped_column(String(200))
    # Nullable: a template need not fix a worker — the one supplied at materialise time wins.
    field_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id"), nullable=True
    )
    recurrence: Mapped[TemplateRecurrence] = mapped_column(
        Enum(TemplateRecurrence, name="template_recurrence")
    )
    # ISO weekday ints (1=Mon .. 7=Sun). Used only when recurrence == WEEKLY (spec §3.3).
    weekdays: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    route_type: Mapped[RouteType] = mapped_column(Enum(RouteType, name="route_type"))
    # Templates are deactivated, never hard-deleted (consistent with Etapa 3 Ruling 3).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    stops: Mapped[list["RouteTemplateStop"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="RouteTemplateStop.order_index",
    )


class RouteTemplateStop(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_template_stops"

    route_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_templates.id")
    )
    service_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_points.id")
    )
    order_index: Mapped[int] = mapped_column(Integer)
    # Time-of-day, not timestamptz: the template has no date. Materialising combines
    # route_date + this time into the route_stop's timestamptz (spec §3.3).
    expected_arrival_from: Mapped[time | None] = mapped_column(Time, nullable=True)
    expected_arrival_to: Mapped[time | None] = mapped_column(Time, nullable=True)

    template: Mapped["RouteTemplate"] = relationship(back_populates="stops")
    service_point: Mapped["ServicePoint"] = relationship()
