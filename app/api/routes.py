"""RF27 (minimum — list of the day's points), RF34 (start)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.catalog.models import FieldWorker
from app.domain.execution.service import RouteAlreadyStarted, start_route
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStop

router = APIRouter(prefix="/routes", tags=["routes"])


class RouteStopOut(BaseModel):
    id: UUID
    order_index: int
    status: str
    service_point_name: str


class RouteOut(BaseModel):
    id: UUID
    field_worker_name: str
    scheduled_start_at: datetime | None
    started_at: datetime | None
    stops: list[RouteStopOut]


class StartRouteRequest(BaseModel):
    latitude: float
    longitude: float
    started_at: datetime


async def _load_route(db: AsyncSession, route_id: UUID) -> Route | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the
    # generic through a direct-return await chained onto .options() and reports it as Any.
    route = await db.scalar(
        select(Route)
        .where(Route.id == route_id)
        .options(
            selectinload(Route.stops).selectinload(RouteStop.service_point),
            selectinload(Route.field_worker),
        )
    )
    return route


def _to_route_out(route: Route) -> RouteOut:
    return RouteOut(
        id=route.id,
        field_worker_name=route.field_worker.full_name,
        scheduled_start_at=route.scheduled_start_at,
        started_at=route.started_at,
        stops=[
            RouteStopOut(
                id=stop.id,
                order_index=stop.order_index,
                status=stop.status.value,
                service_point_name=stop.service_point.name,
            )
            for stop in sorted(route.stops, key=lambda s: s.order_index)
        ],
    )


@router.get("/me", response_model=list[RouteOut])
async def list_my_routes(
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RouteOut]:
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        return []

    routes = (
        await db.scalars(
            select(Route)
            .where(Route.field_worker_id == worker.id)
            .options(
                selectinload(Route.stops).selectinload(RouteStop.service_point),
                selectinload(Route.field_worker),
            )
        )
    ).all()
    return [_to_route_out(route) for route in routes]


@router.get("", response_model=list[RouteOut])
async def list_all_routes(
    _user: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RouteOut]:
    routes = (
        await db.scalars(
            select(Route).options(
                selectinload(Route.stops).selectinload(RouteStop.service_point),
                selectinload(Route.field_worker),
            )
        )
    ).all()
    return [_to_route_out(route) for route in routes]


@router.post("/{route_id}/start", response_model=RouteOut)
async def start_route_endpoint(
    route_id: UUID,
    body: StartRouteRequest,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
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

    return _to_route_out(route)
