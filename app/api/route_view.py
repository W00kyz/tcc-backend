"""The read model for a route, shared by every caller that returns one: the manager reads and
the field-worker feed (`app.api.routes`). Mobile and the dashboard consume the same `RouteOut`
shape so a route looks identical on both (Task 7)."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import Floor, ServicePoint
from app.domain.routing.models import ReleaseReason, Route, RouteStop
from app.domain.routing.service import routing_degraded

# One eager-load recipe shared by every read: RouteOut needs the stop's service point down to
# its building, the route's field worker, and the stop's service type so `service_type_name`
# resolves without a lazy load under async.
_ROUTE_LOADERS = (
    selectinload(Route.stops)
    .selectinload(RouteStop.service_point)
    .selectinload(ServicePoint.floor)
    .selectinload(Floor.building),
    selectinload(Route.field_worker),
    selectinload(Route.stops).selectinload(RouteStop.service_type),
)


class RouteStopOut(BaseModel):
    id: UUID
    order_index: int
    # Which allocation batch handed this stop out; the app shows the current batch's stops.
    batch_number: int
    status: str
    # Why a RELEASED stop went back to the pool; null for every other status.
    release_reason: ReleaseReason | None
    service_point_id: UUID
    service_point_name: str
    floor_label: str
    building_name: str
    latitude: float
    longitude: float
    distance_from_prev_m: float | None
    duration_from_prev_s: float | None
    leg_geometry: list[list[float]] | None
    # The service type executed here; both are null for a stop with no `service_type_id`.
    service_type_id: UUID | None
    service_type_name: str | None


class RouteOut(BaseModel):
    id: UUID
    field_worker_id: UUID
    field_worker_name: str
    route_date: date
    status: str
    started_at: datetime | None
    routing_degraded: bool
    # The latest batch handed out (0 before the first). `stops` carries every batch; the app
    # lists the current batch's stops and counts the done ones.
    current_batch: int
    stops: list[RouteStopOut]


def _to_stop_out(stop: RouteStop) -> RouteStopOut:
    point = stop.service_point
    return RouteStopOut(
        id=stop.id,
        order_index=stop.order_index,
        batch_number=stop.batch_number,
        status=stop.status.value,
        release_reason=stop.release_reason,
        service_point_id=point.id,
        service_point_name=point.name,
        floor_label=point.floor.label,
        building_name=point.floor.building.name,
        latitude=point.latitude,
        longitude=point.longitude,
        distance_from_prev_m=stop.distance_from_prev_m,
        duration_from_prev_s=stop.duration_from_prev_s,
        leg_geometry=stop.leg_geometry,
        service_type_id=stop.service_type_id,
        service_type_name=stop.service_type.name if stop.service_type is not None else None,
    )


def _to_route_out(route: Route) -> RouteOut:
    return RouteOut(
        id=route.id,
        field_worker_id=route.field_worker_id,
        field_worker_name=route.field_worker.full_name,
        route_date=route.route_date,
        status=route.status.value,
        started_at=route.started_at,
        routing_degraded=routing_degraded(route),
        current_batch=max((stop.batch_number for stop in route.stops), default=0),
        # Route.stops is order_by="RouteStop.order_index" and every service function reloads it
        # in that order — no re-sort needed here.
        stops=[_to_stop_out(stop) for stop in route.stops],
    )


async def _load_route(db: AsyncSession, route_id: UUID) -> Route | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the generic
    # through a direct-return await chained onto .options() and reports it as Any.
    route = await db.scalar(select(Route).where(Route.id == route_id).options(*_ROUTE_LOADERS))
    return route
