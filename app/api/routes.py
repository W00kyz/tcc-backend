"""Route CRUD for managers/admins (RF11, RF13, RF14, RF24) plus the two field-worker
endpoints (`GET /routes/me` — RF27 "rota do dia", `POST /routes/{id}/start` — RF34). The
`RouteOut` read model is shared with the template router through `app.api.route_view` so a
route looks the same on mobile and the dashboard. Business rules live in
`app.domain.routing.service` / `app.domain.execution.service`; this router validates input,
owns the transaction, and records the audit trail."""

from datetime import UTC, date, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.api.route_view import (
    _ROUTE_LOADERS,
    RouteOut,
    _load_route,
    _reload,
    _to_route_out,
)
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import FieldWorker
from app.domain.execution.service import (
    RouteAlreadyStarted,
    RouteCancelled,
    RouteNotStartable,
    StopAlreadyDone,
    StopAlreadyManuallyCompleted,
    StopNotOnRoute,
    complete_manually,
    start_route,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteType
from app.domain.routing.osrm import OsrmUnavailable
from app.domain.routing.service import (
    DoneStopRemoved,
    RouteNotEditable,
    StopInput,
    UnknownFieldWorker,
    UnknownServicePoint,
    cancel_route,
    create_route,
    optimize_route,
    reassign_route,
    replace_route_stops,
)

router = APIRouter(prefix="/routes", tags=["routes"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_FieldWorker = Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))]
_Db = Annotated[AsyncSession, Depends(get_db)]


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


class OccasionalRouteBody(BaseModel):
    field_worker_ids: list[UUID] = Field(min_length=1)
    route_date: date
    scheduled_start_at: datetime | None = None
    stops: list[StopBody]


class RouteUpdateBody(BaseModel):
    field_worker_id: UUID | None = None
    route_date: date | None = None
    scheduled_start_at: datetime | None = None
    stops: list[StopBody] | None = None


class ReassignBody(BaseModel):
    field_worker_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class CancelBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class CompleteManuallyBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


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
    except UnknownFieldWorker as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
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


@router.post("/occasional", response_model=list[RouteOut], status_code=status.HTTP_201_CREATED)
async def create_occasional_routes(
    body: OccasionalRouteBody, request: Request, actor: _Manager, db: _Db
) -> list[RouteOut]:
    """RF23 / spec §3.5 Ruling 2 — an occasional route assigned to N workers is N independent
    routes, one per worker, each a copy of the same stops. One transaction: if any worker id
    is unknown, nothing is committed."""
    created: list[Route] = []
    try:
        for field_worker_id in body.field_worker_ids:
            route = await create_route(
                db,
                field_worker_id=field_worker_id,
                route_date=body.route_date,
                route_type=RouteType.OCCASIONAL,
                scheduled_start_at=body.scheduled_start_at,
                stops=[_to_stop_input(stop) for stop in body.stops],
                actor_id=actor.id,
                osrm=request.app.state.osrm_client,
            )
            created.append(route)
    except UnknownFieldWorker as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except UnknownServicePoint as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    for route in created:
        await record_audit_trail(
            db,
            actor_id=actor.id,
            entity_type="route",
            entity_id=route.id,
            action="create",
            before=None,
            after={
                "field_worker_id": str(route.field_worker_id),
                "route_date": body.route_date.isoformat(),
                "route_type": route.route_type.value,
                "stop_count": len(route.stops),
            },
        )
    await db.commit()
    return [_to_route_out(await _reload(db, route.id)) for route in created]


def _parse_enum[FilterEnum: Enum](enum_cls: type[FilterEnum], value: str, param: str) -> FilterEnum:
    """Turn a raw `?route_type=` / `?status=` string into its enum member, or answer 422 naming
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
    route_type: str | None = None,
    route_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[RouteOut]:
    stmt = select(Route).options(*_ROUTE_LOADERS).order_by(Route.route_date, Route.created_at)
    if route_date is not None:
        stmt = stmt.where(Route.route_date == route_date)
    if field_worker_id is not None:
        stmt = stmt.where(Route.field_worker_id == field_worker_id)
    if route_type is not None:
        stmt = stmt.where(Route.route_type == _parse_enum(RouteType, route_type, "route_type"))
    if route_status is not None:
        stmt = stmt.where(Route.status == _parse_enum(RouteStatus, route_status, "status"))
    routes = (await db.scalars(stmt)).all()
    return [_to_route_out(route) for route in routes]


@router.get("/me", response_model=list[RouteOut])
async def list_my_routes(
    user: _FieldWorker,
    db: _Db,
    route_date: Annotated[date | None, Query(alias="date")] = None,
) -> list[RouteOut]:
    """RF27 "rota do dia": the mobile feed for the signed-in worker. Same `RouteOut` shape the
    manager endpoints return, so mobile and the dashboard render a route identically. Without
    `?date=` it defaults to today; CANCELLED routes are never shown."""
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        return []
    stmt = (
        select(Route)
        .where(
            Route.field_worker_id == worker.id,
            Route.route_date == (route_date or date.today()),
            Route.status != RouteStatus.CANCELLED,
        )
        .options(*_ROUTE_LOADERS)
        .order_by(Route.created_at)
    )
    routes = (await db.scalars(stmt)).all()
    return [_to_route_out(route) for route in routes]


@router.get("/{route_id}", response_model=RouteOut)
async def get_route(route_id: UUID, _actor: _Manager, db: _Db) -> RouteOut:
    # MANAGER/ADMIN only — the FIELD_WORKER-owner path is `GET /routes/me`.
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

    # A changed field_worker_id here would bypass the append-only stop_assignments chain that
    # POST /routes/{id}/reassign maintains (spec §3.4 Ruling 1 / RF21), leaving route and
    # assignments pointing at different workers. Reject it before any write; an unchanged value
    # (dashboard edit forms echo it back) is a no-op and is not validated.
    if body.field_worker_id is not None and body.field_worker_id != route.field_worker_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Route "{route_id}" worker cannot be changed here; '
            f"use POST /routes/{route_id}/reassign.",
        )

    before = _route_snapshot(route)
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


@router.post("/{route_id}/optimize", response_model=RouteOut)
async def optimize_route_endpoint(
    route_id: UUID, request: Request, actor: _Manager, db: _Db
) -> RouteOut:
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')

    before = {"stop_order": _stop_order(route)}
    try:
        await optimize_route(db, route=route, osrm=request.app.state.osrm_client)
    except OsrmUnavailable as exc:
        # Unlike a plain save, the optimise endpoint has nothing to show without OSRM — the
        # visiting order IS the OSRM answer (spec §4.2, Ruling 6).
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Route optimization is unavailable: the OSRM service did not respond.",
        ) from exc
    except RouteNotEditable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    reloaded = await _reload(db, route_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route_id,
        action="optimize",
        before=before,
        after={"stop_order": _stop_order(reloaded)},
    )
    await db.commit()
    return _to_route_out(reloaded)


@router.post("/{route_id}/reassign", response_model=RouteOut)
async def reassign_route_endpoint(
    route_id: UUID, body: ReassignBody, actor: _Manager, db: _Db
) -> RouteOut:
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')

    before = {"field_worker_id": str(route.field_worker_id)}
    try:
        await reassign_route(
            db,
            route=route,
            new_field_worker_id=body.field_worker_id,
            reason=body.reason,
            actor_id=actor.id,
        )
    except RouteNotEditable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except UnknownFieldWorker as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    reloaded = await _reload(db, route_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route_id,
        action="reassign",
        before=before,
        after={"field_worker_id": str(body.field_worker_id), "reason": body.reason},
    )
    await db.commit()
    return _to_route_out(reloaded)


@router.post("/{route_id}/cancel", response_model=RouteOut)
async def cancel_route_endpoint(
    route_id: UUID, body: CancelBody, actor: _Manager, db: _Db
) -> RouteOut:
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')

    before = {"status": route.status.value}
    try:
        await cancel_route(db, route=route, reason=body.reason)
    except RouteNotEditable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    reloaded = await _reload(db, route_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route_id,
        action="cancel",
        before=before,
        after={"status": reloaded.status.value, "reason": body.reason},
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

    prior_status = route.status.value
    try:
        route = await start_route(
            db,
            route=route,
            latitude=body.latitude,
            longitude=body.longitude,
            started_at=body.started_at,
        )
    except (RouteAlreadyStarted, RouteNotStartable) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # `start_route` commits internally (the documented execution-module exception); the audit
    # row is appended in a second transaction, exactly as `complete_stop_manually` does.
    assert route.started_at is not None
    await record_audit_trail(
        db,
        actor_id=user.id,
        entity_type="route",
        entity_id=route_id,
        action="start",
        before={"status": prior_status},
        after={"status": route.status.value, "started_at": route.started_at.isoformat()},
    )
    await db.commit()
    return _to_route_out(await _reload(db, route_id))


@router.post(
    "/{route_id}/stops/{stop_id}/complete-manually",
    response_model=RouteOut,
    status_code=status.HTTP_201_CREATED,
)
async def complete_stop_manually(
    route_id: UUID, stop_id: UUID, body: CompleteManuallyBody, actor: _Manager, db: _Db
) -> RouteOut:
    """RF53 — a manager closes a stop the worker could not reach, with a reason and no
    evidence. `complete_manually` owns and commits its own transaction (the execution module
    exception to "service flushes, never commits"); the audit trail is appended in a second
    transaction afterwards, exactly as `start_route` does."""
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')

    stop = await db.scalar(select(RouteStop).where(RouteStop.id == stop_id))
    if stop is None or stop.route_id != route_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f'Route stop "{stop_id}" not found on route "{route_id}".',
        )

    prior_status = stop.status.value
    try:
        await complete_manually(
            db,
            route=route,
            stop=stop,
            actor_id=actor.id,
            reason=body.reason,
            completed_at=datetime.now(UTC),
        )
    except StopNotOnRoute as exc:  # defensive — the check above already 404s a mismatched pair
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (RouteCancelled, StopAlreadyDone, StopAlreadyManuallyCompleted) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route_stop",
        entity_id=stop_id,
        action="complete_manually",
        before={"status": prior_status},
        after={"status": "DONE", "reason": body.reason},
    )
    await db.commit()
    return _to_route_out(await _reload(db, route_id))


def _route_snapshot(route: Route) -> dict[str, object]:
    return {
        "field_worker_id": str(route.field_worker_id),
        "route_date": route.route_date.isoformat(),
        "route_type": route.route_type.value,
        "status": route.status.value,
        "stop_count": len(route.stops),
    }


def _stop_order(route: Route) -> list[str]:
    """The visiting order as service-point ids — the audit `before`/`after` for optimise, whose
    only effect is a reordering."""
    return [str(stop.service_point_id) for stop in route.stops]
