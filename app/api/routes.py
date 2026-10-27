"""Routes: the manager list and detail (RF41), the field worker's day (`GET /routes/me` — RF27)
and the automatic allocation that creates them (`POST /routes/start`, `POST
/routes/{id}/next-batch` — spec 2026-10-20-alocacao-automatica §6.2); nobody builds a route by
hand. The `RouteOut` read model lives in `app.api.route_view` so a route looks the same on mobile
and the dashboard."""

from collections.abc import Coroutine
from datetime import date, datetime
from enum import Enum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ApprovedWorker,
    CampusToday,
    Now,
    ensure_location_consent,
    require_role,
)
from app.api.route_view import _ROUTE_LOADERS, RouteOut, _load_route, _to_route_out
from app.core.campus_clock import campus_today
from app.db.session import get_db
from app.domain.identity.models import User, UserRole
from app.domain.routing.allocation import (
    AllocationRefused,
    AllocationResult,
    NotAServiceDay,
    OutsideCampus,
    RouteAlreadyDone,
    WorkdayOver,
    next_batch,
    start_or_resume,
)
from app.domain.routing.models import Route, RouteStatus
from app.domain.settings.campus import is_service_day
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/routes", tags=["routes"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

_REFUSAL_CODES: dict[type[AllocationRefused], str] = {
    NotAServiceDay: "NOT_A_SERVICE_DAY",
    WorkdayOver: "WORKDAY_OVER",
    OutsideCampus: "OUTSIDE_CAMPUS",
    RouteAlreadyDone: "ROUTE_DONE",
}


class AllocationBody(BaseModel):
    """Where the worker is when asking for a batch: the nearest free points are measured from
    here, and it must be inside the campus."""

    latitude: float
    longitude: float
    # Device clock of the fix — informational; the server clock decides the day and the hour.
    captured_at: datetime


class AllocationOut(BaseModel):
    route: RouteOut
    # True when nothing could be handed out. An empty pool leaves the route DONE, yet not for
    # good: asking again the same workday reopens it when a point went back to the pool.
    pool_empty: bool


class MyDayOut(BaseModel):
    """The worker's day for the app: today's route, if any, and the location-silence limits it
    warns the worker about (spec §7.3)."""

    service_day: bool
    route: RouteOut | None
    position_report_interval_seconds: int
    location_silence_warning_minutes: int
    location_silence_release_minutes: int


def _parse_enum[FilterEnum: Enum](enum_cls: type[FilterEnum], value: str, param: str) -> FilterEnum:
    """Turn a raw `?status=` string into its enum member, or answer 422 naming
    the offending value and the allowed set — an unknown filter is a client error, not an
    excuse to silently return an empty list (Task-4 review)."""
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(str(member.value) for member in enum_cls.__members__.values())
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f'Invalid {param} "{value}"; expected one of [{allowed}].',
        ) from exc


@router.get("", response_model=list[RouteOut])
async def list_routes(
    _actor: _Manager,
    db: _Db,
    route_date: Annotated[date | None, Query(alias="date")] = None,
    field_worker_id: UUID | None = None,
    route_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[RouteOut]:
    stmt = select(Route).options(*_ROUTE_LOADERS).order_by(Route.route_date, Route.created_at)
    if route_date is not None:
        stmt = stmt.where(Route.route_date == route_date)
    if field_worker_id is not None:
        stmt = stmt.where(Route.field_worker_id == field_worker_id)
    if route_status is not None:
        stmt = stmt.where(Route.status == _parse_enum(RouteStatus, route_status, "status"))
    routes = (await db.scalars(stmt)).all()
    return [_to_route_out(route) for route in routes]


@router.get("/me", response_model=MyDayOut)
async def my_day(worker: ApprovedWorker, db: _Db, today: CampusToday) -> MyDayOut:
    """RF27 "rota do dia": the signed-in worker's route of today (or none yet). Same `RouteOut`
    shape the manager endpoints return, so mobile and the dashboard render a route identically."""
    settings = await get_settings_row(db)
    route = await db.scalar(
        select(Route)
        .where(Route.field_worker_id == worker.id, Route.route_date == today)
        .options(*_ROUTE_LOADERS)
    )
    return MyDayOut(
        service_day=is_service_day(settings, today),
        route=_to_route_out(route) if route is not None else None,
        position_report_interval_seconds=settings.position_report_interval_seconds,
        location_silence_warning_minutes=settings.location_silence_warning_minutes,
        location_silence_release_minutes=settings.location_silence_release_minutes,
    )


@router.post("/start", response_model=AllocationOut)
async def start_route(
    body: AllocationBody, worker: ApprovedWorker, db: _Db, now: Now, request: Request
) -> AllocationOut:
    """The "Iniciar rota" button: create or reopen today's route and hand out a batch; a retry
    while the batch is open returns it unchanged. A route DONE for an empty pool is looked at
    again: reopened with the points released since, else `pool_empty=true`. 409 with a machine
    code when the day or the position refuses any batch."""
    await ensure_location_consent(db, worker)
    settings = await get_settings_row(db)
    result = await _allocating(
        start_or_resume(
            db,
            worker=worker,
            today=campus_today(now),
            now=now,
            lat=body.latitude,
            lng=body.longitude,
            settings=settings,
            osrm=request.app.state.osrm_client,
        )
    )
    return await _allocation_out(db, result)


@router.post("/{route_id}/next-batch", response_model=AllocationOut)
async def next_route_batch(
    route_id: UUID,
    body: AllocationBody,
    worker: ApprovedWorker,
    db: _Db,
    now: Now,
    request: Request,
) -> AllocationOut:
    """The next batch once the current one has no open stop (unchanged while it has one). An
    empty pool closes the route: `pool_empty=true`, status DONE; asking again the same workday
    reopens it when a point went back to the pool. 409 `ROUTE_DONE` for a route of a past
    day."""
    await ensure_location_consent(db, worker)
    route = await db.get(Route, route_id)
    if route is None or route.field_worker_id != worker.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    settings = await get_settings_row(db)
    result = await _allocating(
        next_batch(
            db,
            route=route,
            now=now,
            lat=body.latitude,
            lng=body.longitude,
            settings=settings,
            osrm=request.app.state.osrm_client,
        )
    )
    return await _allocation_out(db, result)


async def _allocating(allocation: Coroutine[None, None, AllocationResult]) -> AllocationResult:
    """Await an allocation, turning its refusal into a 409 with the machine code."""
    try:
        return await allocation
    except AllocationRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": _REFUSAL_CODES[type(exc)], "detail": str(exc)},
        ) from exc


async def _allocation_out(db: AsyncSession, result: AllocationResult) -> AllocationOut:
    """Commit the batch (releasing the claim's row locks), then read the route back whole."""
    await db.commit()
    route = await _load_route(db, result.route.id)
    assert route is not None  # just written in this session
    return AllocationOut(route=_to_route_out(route), pool_empty=result.pool_empty)


@router.get("/{route_id}", response_model=RouteOut)
async def get_route(route_id: UUID, _actor: _Manager, db: _Db) -> RouteOut:
    # MANAGER/ADMIN only — the FIELD_WORKER-owner path is `GET /routes/me`.
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    return _to_route_out(route)
