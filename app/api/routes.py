"""Route CRUD for managers/admins (RF11, RF13, RF14, RF24) plus the two field-worker
endpoints kept from Etapa 2 (`GET /routes/me`, `POST /routes/{id}/start`) until Task 7
rewrites them. Business rules live in `app.domain.routing.service`; this router validates
input, owns the transaction, and records the audit trail."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import FieldWorker, Floor, ServicePoint
from app.domain.execution.service import RouteAlreadyStarted, start_route
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteType
from app.domain.routing.service import (
    DoneStopRemoved,
    RouteNotEditable,
    StopInput,
    UnknownServicePoint,
    create_route,
    replace_route_stops,
    routing_degraded,
)

router = APIRouter(prefix="/routes", tags=["routes"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_FieldWorker = Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))]
_Db = Annotated[AsyncSession, Depends(get_db)]

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


class StopBody(BaseModel):
    service_point_id: UUID
    expected_arrival_from: datetime | None = None
    expected_arrival_to: datetime | None = None


class RouteCreateBody(BaseModel):
    field_worker_id: UUID
    route_date: date
    route_type: Literal["REGULAR", "OCCASIONAL"] = "REGULAR"
    scheduled_start_at: datetime | None = None
    stops: list[StopBody]


class RouteUpdateBody(BaseModel):
    field_worker_id: UUID | None = None
    route_date: date | None = None
    scheduled_start_at: datetime | None = None
    stops: list[StopBody] | None = None


class StartRouteRequest(BaseModel):
    latitude: float
    longitude: float
    started_at: datetime


def _to_stop_input(body: StopBody) -> StopInput:
    return StopInput(
        service_point_id=body.service_point_id,
        expected_arrival_from=body.expected_arrival_from,
        expected_arrival_to=body.expected_arrival_to,
    )


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
        stops=[_to_stop_out(stop) for stop in sorted(route.stops, key=lambda s: s.order_index)],
    )


async def _load_route(db: AsyncSession, route_id: UUID) -> Route | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the generic
    # through a direct-return await chained onto .options() and reports it as Any.
    route = await db.scalar(select(Route).where(Route.id == route_id).options(*_ROUTE_LOADERS))
    return route


@router.post("", response_model=RouteOut, status_code=status.HTTP_201_CREATED)
async def create_route_endpoint(
    body: RouteCreateBody, request: Request, actor: _Manager, db: _Db
) -> RouteOut:
    try:
        route = await create_route(
            db,
            field_worker_id=body.field_worker_id,
            route_date=body.route_date,
            route_type=RouteType(body.route_type),
            scheduled_start_at=body.scheduled_start_at,
            stops=[_to_stop_input(stop) for stop in body.stops],
            actor_id=actor.id,
            osrm=request.app.state.osrm_client,
        )
    except UnknownServicePoint as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route.id,
        action="create",
        before=None,
        after={
            "field_worker_id": str(body.field_worker_id),
            "route_date": body.route_date.isoformat(),
            "route_type": route.route_type.value,
            "stop_count": len(route.stops),
        },
    )
    await db.commit()
    return _to_route_out(await _reload(db, route.id))


@router.get("", response_model=list[RouteOut])
async def list_routes(
    _actor: _Manager,
    db: _Db,
    route_date: Annotated[date | None, Query(alias="date")] = None,
    field_worker_id: UUID | None = None,
    route_type: str | None = None,
    route_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[RouteOut]:
    stmt = select(Route).options(*_ROUTE_LOADERS).order_by(Route.route_date, Route.created_at)
    if route_date is not None:
        stmt = stmt.where(Route.route_date == route_date)
    if field_worker_id is not None:
        stmt = stmt.where(Route.field_worker_id == field_worker_id)
    if route_type is not None:
        try:
            stmt = stmt.where(Route.route_type == RouteType(route_type))
        except ValueError:
            return []
    if route_status is not None:
        try:
            stmt = stmt.where(Route.status == RouteStatus(route_status))
        except ValueError:
            return []
    routes = (await db.scalars(stmt)).all()
    return [_to_route_out(route) for route in routes]


@router.get("/me", response_model=list[RouteOut])
async def list_my_routes(user: _FieldWorker, db: _Db) -> list[RouteOut]:
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        return []
    routes = (
        await db.scalars(
            select(Route).where(Route.field_worker_id == worker.id).options(*_ROUTE_LOADERS)
        )
    ).all()
    return [_to_route_out(route) for route in routes]


@router.get("/{route_id}", response_model=RouteOut)
async def get_route(route_id: UUID, _actor: _Manager, db: _Db) -> RouteOut:
    # MANAGER/ADMIN only for now — the FIELD_WORKER-owner path is Task 7's `GET /routes/me`.
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    return _to_route_out(route)


@router.patch("/{route_id}", response_model=RouteOut)
async def patch_route(
    route_id: UUID, body: RouteUpdateBody, request: Request, actor: _Manager, db: _Db
) -> RouteOut:
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    if route.status in (RouteStatus.CANCELLED, RouteStatus.DONE):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Route "{route_id}" is {route.status.value} and cannot be edited.',
        )

    before = _route_snapshot(route)
    if body.field_worker_id is not None:
        route.field_worker_id = body.field_worker_id
    if body.route_date is not None:
        route.route_date = body.route_date
    if body.scheduled_start_at is not None:
        route.scheduled_start_at = body.scheduled_start_at
    if body.stops is not None:
        try:
            await replace_route_stops(
                db,
                route=route,
                stops=[_to_stop_input(stop) for stop in body.stops],
                actor_id=actor.id,
                osrm=request.app.state.osrm_client,
            )
        except RouteNotEditable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except (DoneStopRemoved, UnknownServicePoint) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    await db.flush()
    reloaded = await _reload(db, route_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route_id,
        action="update",
        before=before,
        after=_route_snapshot(reloaded),
    )
    await db.commit()
    return _to_route_out(reloaded)


@router.post("/{route_id}/start", response_model=RouteOut)
async def start_route_endpoint(
    route_id: UUID, body: StartRouteRequest, user: _FieldWorker, db: _Db
) -> RouteOut:
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')

    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None or route.field_worker_id != worker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This route is not assigned to you.")

    try:
        route = await start_route(
            db,
            route=route,
            latitude=body.latitude,
            longitude=body.longitude,
            started_at=body.started_at,
        )
    except RouteAlreadyStarted as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _to_route_out(await _reload(db, route_id))


def _route_snapshot(route: Route) -> dict[str, object]:
    return {
        "field_worker_id": str(route.field_worker_id),
        "route_date": route.route_date.isoformat(),
        "route_type": route.route_type.value,
        "status": route.status.value,
        "stop_count": len(route.stops),
    }


async def _reload(db: AsyncSession, route_id: UUID) -> Route:
    route = await _load_route(db, route_id)
    # The route was just created or mutated inside this same transaction — it exists.
    assert route is not None
    return route
