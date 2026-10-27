"""The impediment surface (RF16-RF17, spec 2026-10-20-alocacao-automatica §7.1, §8.4): a field
worker reports an impediment against their route or one of its stops — the point(s) go back to
the pool at once — and a manager lists the impediments and acknowledges them ("ciente").
Reassigning or dismissing by hand went with manual planning (§2.1).

Every endpoint owns its transaction boundary — call the flush-only service, then
`db.commit()` — the same discipline as `app/api/forms.py`. The impediment services flush,
never commit."""

from datetime import datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApprovedWorker, Now, require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.impediments.service import (
    RouteNotInProgress,
    StopNotReassignable,
    acknowledge_impediment,
    create_impediment,
    find_impediments,
    get_impediment,
)
from app.domain.routing.models import Route, RouteStop

router = APIRouter(tags=["impediments"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Every impediment-service failure the report endpoint translates to an HTTP status by exact
# type.
_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    RouteNotInProgress: status.HTTP_409_CONFLICT,
    StopNotReassignable: status.HTTP_409_CONFLICT,
}


class ImpedimentReportBody(BaseModel):
    route_stop_id: UUID | None = None
    scope: ImpedimentScope
    reason: ImpedimentReason
    # Bounded to match the `impediments.note` column (`String(500)`) and spec §3 —
    # an over-long note would otherwise reach Postgres and raise a 500.
    note: str | None = Field(default=None, max_length=500)
    reported_at: datetime


class ImpedimentOut(BaseModel):
    id: UUID
    route_id: UUID
    route_stop_id: UUID | None
    scope: ImpedimentScope
    reason: ImpedimentReason
    note: str | None
    status: ImpedimentStatus
    reported_at: datetime
    received_at: datetime
    field_worker_id: UUID
    field_worker_name: str
    # None for a ROUTE_REMAINDER report, which names no point.
    service_point_name: str | None
    building_name: str | None
    resolved_at: datetime | None


def _raise_for_domain_error(exc: Exception) -> NoReturn:
    raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc


def _to_out(impediment: Impediment) -> ImpedimentOut:
    """Expects the relationships `find_impediments`/`get_impediment` load."""
    point = impediment.route_stop.service_point if impediment.route_stop is not None else None
    return ImpedimentOut(
        id=impediment.id,
        route_id=impediment.route_id,
        route_stop_id=impediment.route_stop_id,
        scope=impediment.scope,
        reason=impediment.reason,
        note=impediment.note,
        status=impediment.status,
        reported_at=impediment.reported_at,
        received_at=impediment.received_at,
        field_worker_id=impediment.reported_by,
        field_worker_name=impediment.reported_by_worker.full_name,
        service_point_name=point.name if point is not None else None,
        building_name=point.floor.building.name if point is not None else None,
        resolved_at=impediment.resolved_at,
    )


async def _load_impediment(db: AsyncSession, impediment_id: UUID) -> Impediment:
    impediment = await get_impediment(db, impediment_id)
    if impediment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Impediment "{impediment_id}" not found.')
    return impediment


@router.post(
    "/routes/{route_id}/impediments",
    response_model=ImpedimentOut,
    status_code=status.HTTP_201_CREATED,
)
async def report_impediment(
    route_id: UUID,
    body: ImpedimentReportBody,
    request: Request,
    worker: ApprovedWorker,
    db: _Db,
    now: Now,
) -> ImpedimentOut:
    """RF16-RF17 — the field worker signals they cannot execute this stop (scope=STOP) or the
    rest of the route (scope=ROUTE_REMAINDER); what it covers goes back to the pool and the
    managers are alerted and notified. The report, and the released stops' `release_reason`,
    are the record — no audit-trail entry is appended here."""
    route = await db.get(Route, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route "{route_id}" not found.')
    if route.field_worker_id != worker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This route is not assigned to you.")

    route_stop: RouteStop | None = None
    if body.route_stop_id is not None:
        route_stop = await db.get(RouteStop, body.route_stop_id)
        if route_stop is None or route_stop.route_id != route_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f'Route stop "{body.route_stop_id}" not found on route "{route_id}".',
            )

    try:
        impediment = await create_impediment(
            db,
            route=route,
            route_stop=route_stop,
            scope=body.scope,
            reason=body.reason,
            note=body.note,
            reported_by_id=worker.id,
            reported_at=body.reported_at,
            now=now,
            push=request.app.state.push_sender,
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await db.commit()
    return _to_out(await _load_impediment(db, impediment.id))


@router.get("/impediments", response_model=list[ImpedimentOut])
async def list_impediments(
    _actor: _Manager,
    db: _Db,
    impediment_status: Annotated[ImpedimentStatus | None, Query(alias="status")] = None,
) -> list[ImpedimentOut]:
    """The manager's Justificativas tab (spec §8.4): every impediment, PENDING first, or only
    those of `?status=`."""
    return [_to_out(i) for i in await find_impediments(db, status=impediment_status)]


@router.post("/impediments/{impediment_id}/acknowledge", response_model=ImpedimentOut)
async def acknowledge(impediment_id: UUID, actor: _Manager, db: _Db) -> ImpedimentOut:
    """The manager's "ciente": PENDING → RESOLVED, audited. Idempotent on a RESOLVED one."""
    impediment = await _load_impediment(db, impediment_id)
    before = {"status": impediment.status.value}
    impediment = await acknowledge_impediment(db, impediment=impediment, actor=actor)
    if before["status"] != impediment.status.value:
        await record_audit_trail(
            db,
            actor_id=actor.id,
            entity_type="impediment",
            entity_id=impediment.id,
            action="acknowledge",
            before=before,
            after={"status": impediment.status.value},
        )
    await db.commit()
    return _to_out(impediment)
