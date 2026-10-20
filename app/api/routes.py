"""Route reads: the manager list and detail (RF41) and the field worker's "rota do dia"
(`GET /routes/me` — RF27). Routes are created by the automatic allocation, never by hand (spec
2026-10-20-alocacao-automatica §2.1). The `RouteOut` read model lives in `app.api.route_view` so
a route looks the same on mobile and the dashboard."""

from datetime import date
from enum import Enum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CampusToday, require_role
from app.api.route_view import _ROUTE_LOADERS, RouteOut, _load_route, _to_route_out
from app.db.session import get_db
from app.domain.catalog.models import FieldWorker
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus

router = APIRouter(prefix="/routes", tags=["routes"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_FieldWorker = Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))]
_Db = Annotated[AsyncSession, Depends(get_db)]


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
    return [await _to_route_out(db, route) for route in routes]


@router.get("/me", response_model=list[RouteOut])
async def list_my_routes(
    user: _FieldWorker,
    db: _Db,
    today: CampusToday,
    route_date: Annotated[date | None, Query(alias="date")] = None,
) -> list[RouteOut]:
    """RF27 "rota do dia": the mobile feed for the signed-in worker. Same `RouteOut` shape the
    manager endpoints return, so mobile and the dashboard render a route identically. Without
    `?date=` it defaults to today."""
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        return []
    stmt = (
        select(Route)
        .where(
            Route.field_worker_id == worker.id,
            Route.route_date == (route_date or today),
        )
        .options(*_ROUTE_LOADERS)
        .order_by(Route.created_at)
    )
    routes = (await db.scalars(stmt)).all()
    return [await _to_route_out(db, route) for route in routes]


@router.get("/{route_id}", response_model=RouteOut)
async def get_route(route_id: UUID, _actor: _Manager, db: _Db) -> RouteOut:
    # MANAGER/ADMIN only — the FIELD_WORKER-owner path is `GET /routes/me`.
    route = await _load_route(db, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    return await _to_route_out(db, route)
