"""Etapa 8 — the impediment surface: a field worker reports an impediment against their
route or one of its stops (RF16-RF17); a manager works the PENDING queue, ranks
reassignment candidates and either reassigns the affected stop(s) to another professional
(RF18-RF21) or dismisses the report.

Every endpoint owns its transaction boundary — call the flush-only service, append an
audit-trail entry when the action mutates an assignment, then `db.commit()` — the same
discipline as `app/api/forms.py`. The impediment services flush, never commit."""

from datetime import datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._execution_common import current_worker
from app.api.deps import require_role
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
    AlreadyResolved,
    ImpedimentNotFound,
    NoAffectedStops,
    NoRouteForDate,
    RouteNotInProgress,
    SameRouteReassignment,
    candidates_for,
    create_impediment,
    dismiss_impediment,
    list_pending,
    reassign_impediment,
)
from app.domain.impediments.service import StopNotReassignable as ImpedimentStopNotReassignable
from app.domain.routing.models import Route, RouteStop
from app.domain.routing.service import (
    RouteNotEditable,
    StopNotOnRoute,
    UnknownFieldWorker,
)
from app.domain.routing.service import StopNotReassignable as RoutingStopNotReassignable

router = APIRouter(tags=["impediments"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_FieldWorker = Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Every impediment-service and reassign-stops failure the endpoints translate to an HTTP
# status by exact type. Both `StopNotReassignable` variants (the impediment guard at report
# time and the `reassign_stops` guard at move time) mean the same thing to a caller — a stop
# that can no longer move (it is DONE) — so both land on 409.
_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    ImpedimentNotFound: status.HTTP_404_NOT_FOUND,
    RouteNotInProgress: status.HTTP_409_CONFLICT,
    ImpedimentStopNotReassignable: status.HTTP_409_CONFLICT,
    AlreadyResolved: status.HTTP_409_CONFLICT,
    NoAffectedStops: status.HTTP_409_CONFLICT,
    NoRouteForDate: status.HTTP_409_CONFLICT,
    SameRouteReassignment: status.HTTP_409_CONFLICT,
    RoutingStopNotReassignable: status.HTTP_409_CONFLICT,
    StopNotOnRoute: status.HTTP_409_CONFLICT,
    RouteNotEditable: status.HTTP_409_CONFLICT,
    UnknownFieldWorker: status.HTTP_404_NOT_FOUND,
}


class ImpedimentReportBody(BaseModel):
    route_stop_id: UUID | None = None
    scope: ImpedimentScope
    reason: ImpedimentReason
    # Bounded to match the `impediments.note` column (`String(500)`) and spec §3 —
    # an over-long note would otherwise reach Postgres and raise a 500.
    note: str | None = Field(default=None, max_length=500)
    reported_at: datetime


class ImpedimentReassignBody(BaseModel):
    field_worker_id: UUID
    reason: str = Field(min_length=1, max_length=300)


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


class ReassignCandidateOut(BaseModel):
    field_worker_id: UUID
    full_name: str
    distance_m: float
    pending_stop_count: int


def _raise_for_domain_error(exc: Exception) -> NoReturn:
    raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc


def _to_out(impediment: Impediment) -> ImpedimentOut:
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
    )


async def _load_impediment(db: AsyncSession, impediment_id: UUID) -> Impediment:
    """Fetch the impediment or raise `ImpedimentNotFound` (declared in Task 2 for exactly
    this call site), which the endpoint's handler maps to 404."""
    impediment = await db.get(Impediment, impediment_id)
    if impediment is None:
        raise ImpedimentNotFound(f'Impediment "{impediment_id}" not found.')
    return impediment


@router.post(
    "/routes/{route_id}/impediments",
    response_model=ImpedimentOut,
    status_code=status.HTTP_201_CREATED,
)
async def report_impediment(
    route_id: UUID, body: ImpedimentReportBody, user: _FieldWorker, db: _Db
) -> ImpedimentOut:
    """RF16-RF17 — the field worker signals they cannot execute this stop (scope=STOP) or the
    rest of the route (scope=ROUTE_REMAINDER). The report is the record; no audit-trail entry
    is appended here (nothing on the assignment chain moved yet)."""
    worker = await current_worker(db, user)
    if worker is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no field worker profile.")

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
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await db.commit()
    await db.refresh(impediment)
    return _to_out(impediment)


@router.get("/impediments", response_model=list[ImpedimentOut])
async def list_impediments(
    _actor: _Manager,
    db: _Db,
    impediment_status: Annotated[str, Query(alias="status")] = ImpedimentStatus.PENDING.value,
) -> list[ImpedimentOut]:
    """The manager's queue. Only the PENDING queue is served — the impediment domain keeps no
    other list — so any other `?status=` value is a client error, not an empty list."""
    if impediment_status != ImpedimentStatus.PENDING.value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f'Invalid status "{impediment_status}"; expected "{ImpedimentStatus.PENDING.value}".',
        )
    return [_to_out(impediment) for impediment in await list_pending(db)]


@router.get("/impediments/{impediment_id}/candidates", response_model=list[ReassignCandidateOut])
async def list_candidates(
    impediment_id: UUID, _actor: _Manager, db: _Db
) -> list[ReassignCandidateOut]:
    try:
        impediment = await _load_impediment(db, impediment_id)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    ranked = await candidates_for(db, impediment=impediment)
    return [
        ReassignCandidateOut(
            field_worker_id=candidate.field_worker_id,
            full_name=candidate.full_name,
            distance_m=candidate.distance_m,
            pending_stop_count=candidate.pending_stop_count,
        )
        for candidate in ranked
    ]


@router.post("/impediments/{impediment_id}/reassign", response_model=ImpedimentOut)
async def reassign(
    impediment_id: UUID, body: ImpedimentReassignBody, request: Request, actor: _Manager, db: _Db
) -> ImpedimentOut:
    """RF18-RF21 — move the impediment's affected stop(s) onto `field_worker_id`'s route for
    the same date and mark the impediment RESOLVED. `reassign_impediment` delegates the move
    to `reassign_stops`, which appends its own stop-assignment rows; this endpoint adds one
    audit-trail entry naming the manager's decision."""
    try:
        impediment = await _load_impediment(db, impediment_id)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)

    before = {"status": impediment.status.value}
    try:
        impediment = await reassign_impediment(
            db,
            impediment=impediment,
            new_field_worker_id=body.field_worker_id,
            reason=body.reason,
            actor_id=actor.id,
            osrm=request.app.state.osrm_client,
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)

    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="impediment",
        entity_id=impediment.id,
        action="reassign",
        before=before,
        after={
            "status": impediment.status.value,
            "field_worker_id": str(body.field_worker_id),
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(impediment)
    return _to_out(impediment)


@router.post("/impediments/{impediment_id}/dismiss", response_model=ImpedimentOut)
async def dismiss(impediment_id: UUID, actor: _Manager, db: _Db) -> ImpedimentOut:
    """Close the impediment with no reassignment (RF17) — a duplicate report, or one the
    manager resolved off-system."""
    try:
        impediment = await _load_impediment(db, impediment_id)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)

    before = {"status": impediment.status.value}
    try:
        impediment = await dismiss_impediment(db, impediment=impediment, actor_id=actor.id)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)

    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="impediment",
        entity_id=impediment.id,
        action="dismiss",
        before=before,
        after={"status": impediment.status.value},
    )
    await db.commit()
    await db.refresh(impediment)
    return _to_out(impediment)
