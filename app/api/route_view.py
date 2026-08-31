"""The read model for a route, shared by every caller that returns one: the manager CRUD
endpoints (`app.api.routes`), the template materialise endpoint (`app.api.route_templates`)
and the field-worker feed. Mobile and the dashboard consume the same `RouteOut` shape so a
route looks identical on both (Task 7). Request-side helpers (`StopInput` mapping) stay in the
routers; only the response side lives here."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import Floor, ServicePoint
from app.domain.routing.models import Route, RouteStop
from app.domain.routing.service import routing_degraded

# One eager-load recipe shared by every read: RouteOut needs the stop's service point down to
# its building, plus the route's field worker, and lazy-loading is impossible under async.
_ROUTE_LOADERS = (
    selectinload(Route.stops)
    .selectinload(RouteStop.service_point)
    .selectinload(ServicePoint.floor)
    .selectinload(Floor.building),
    selectinload(Route.field_worker),
)


class RouteStopOut(BaseModel):
    id: UUID
    order_index: int
    status: str
    service_point_id: UUID
    service_point_name: str
    floor_label: str
    building_name: str
    latitude: float
    longitude: float
    point_type: str  # REGULAR / OCCASIONAL (RF24)
    expected_arrival_from: datetime | None
    expected_arrival_to: datetime | None
    distance_from_prev_m: float | None
    duration_from_prev_s: float | None
    leg_geometry: list[list[float]] | None


class RouteOut(BaseModel):
    id: UUID
    field_worker_id: UUID
    field_worker_name: str
    route_date: date
    route_type: str
    status: str
    scheduled_start_at: datetime | None
    started_at: datetime | None
    routing_degraded: bool
    stops: list[RouteStopOut]


def _to_stop_out(stop: RouteStop) -> RouteStopOut:
    point = stop.service_point
    return RouteStopOut(
        id=stop.id,
        order_index=stop.order_index,
        status=stop.status.value,
        service_point_id=point.id,
        service_point_name=point.name,
        floor_label=point.floor.label,
        building_name=point.floor.building.name,
        latitude=point.latitude,
        longitude=point.longitude,
        point_type=point.point_type.value,
        expected_arrival_from=stop.expected_arrival_from,
        expected_arrival_to=stop.expected_arrival_to,
        distance_from_prev_m=stop.distance_from_prev_m,
        duration_from_prev_s=stop.duration_from_prev_s,
        leg_geometry=stop.leg_geometry,
    )


def _to_route_out(route: Route) -> RouteOut:
    return RouteOut(
        id=route.id,
        field_worker_id=route.field_worker_id,
        field_worker_name=route.field_worker.full_name,
        route_date=route.route_date,
        route_type=route.route_type.value,
        status=route.status.value,
        scheduled_start_at=route.scheduled_start_at,
        started_at=route.started_at,
        routing_degraded=routing_degraded(route),
        # Route.stops is order_by="RouteStop.order_index" and every service function reloads it
        # in that order — no re-sort needed here.
        stops=[_to_stop_out(stop) for stop in route.stops],
    )


async def _load_route(db: AsyncSession, route_id: UUID) -> Route | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the generic
    # through a direct-return await chained onto .options() and reports it as Any.
    route = await db.scalar(select(Route).where(Route.id == route_id).options(*_ROUTE_LOADERS))
    return route


async def _reload(db: AsyncSession, route_id: UUID) -> Route:
    route = await _load_route(db, route_id)
    # The route was just created or mutated inside this same transaction — it exists.
    assert route is not None
    return route
