"""Etapa 8 — the impediment surface: a field worker reports an impediment against their
route or one of its stops (RF16-RF17) and a manager reads the PENDING queue. Reassigning or
dismissing by hand went with manual planning (spec 2026-10-20-alocacao-automatica §2.1).

Every endpoint owns its transaction boundary — call the flush-only service, then
`db.commit()` — the same discipline as `app/api/forms.py`. The impediment services flush,
never commit."""

from datetime import datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApprovedWorker, require_role
from app.db.session import get_db
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
    create_impediment,
    list_pending,
)
from app.domain.monitoring.notifications import notify_impediment_reported
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


@router.post(
    "/routes/{route_id}/impediments",
    response_model=ImpedimentOut,
    status_code=status.HTTP_201_CREATED,
)
async def report_impediment(
    route_id: UUID, body: ImpedimentReportBody, request: Request, worker: ApprovedWorker, db: _Db
) -> ImpedimentOut:
    """RF16-RF17 — the field worker signals they cannot execute this stop (scope=STOP) or the
    rest of the route (scope=ROUTE_REMAINDER). The report is the record; no audit-trail entry
    is appended here (nothing on the assignment chain moved yet)."""
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
    # RF17: the managers learn about it in the notification center, not only by polling the
    # impediment queue.
    await notify_impediment_reported(
        db,
        impediment_id=impediment.id,
        route=route,
        route_stop=route_stop,
        reason=body.reason.value,
        push=request.app.state.push_sender,
    )
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
