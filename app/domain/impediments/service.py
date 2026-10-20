# ruff: noqa: N818 — RouteNotInProgress / StopNotReassignable name the signal
# condition the caller catches by name, same convention as app/domain/forms/service.py.
"""Impediment reports service: register a field worker's impediment and list the PENDING queue
for the manager (RF16-RF17, Etapa 8). Ranking candidates and reassigning by hand went with
manual planning (spec 2026-10-20-alocacao-automatica §2.1).

Transaction discipline: these functions flush, never commit — the endpoint owns the
transaction boundary, same convention as app/domain/forms/service.py."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus


class ImpedimentError(Exception):
    """Base class for every impediment-service failure."""


class RouteNotInProgress(ImpedimentError):
    """The route is not IN_PROGRESS, so no impediment can be reported against it."""


class StopNotReassignable(ImpedimentError):
    """A STOP-scope impediment targets a stop that can no longer move — it is neither PENDING
    (never reached) nor IN_PROGRESS (checked in, stuck); a DONE stop is finished work."""


async def create_impediment(
    db: AsyncSession,
    *,
    route: Route,
    route_stop: RouteStop | None,
    scope: ImpedimentScope,
    reason: ImpedimentReason,
    note: str | None,
    reported_by_id: uuid.UUID,
    reported_at: datetime,
) -> Impediment:
    """Register a worker's impediment against a route (ROUTE_REMAINDER) or one of its
    stops (STOP).

    Raises RouteNotInProgress unless `route.status == IN_PROGRESS`. For scope=STOP also
    raises StopNotReassignable unless `route_stop.status` is PENDING (never reached) or
    IN_PROGRESS (checked in, then hit a wall) — a worker who checked in and then can't
    finish must still be able to signal it. A DONE stop is finished work and cannot move.
    A ROUTE_REMAINDER report validates no individual stop; the route may have zero pending
    stops right now and the request is still recorded."""
    if route.status != RouteStatus.IN_PROGRESS:
        raise RouteNotInProgress(
            f"Route {route.id} has status {route.status}; expected IN_PROGRESS."
        )
    if scope == ImpedimentScope.STOP and (
        route_stop is None
        or route_stop.status not in (RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS)
    ):
        actual = route_stop.status if route_stop is not None else None
        raise StopNotReassignable(
            f"Route stop {route_stop and route_stop.id} has status {actual}; "
            f"expected PENDING or IN_PROGRESS."
        )
    # spec §3: `route_stop_id` is meaningless for a ROUTE_REMAINDER report ("nulo
    # quando scope=ROUTE_REMAINDER"). Drop any stop the caller passed rather than
    # reject it — a stray id is harmless to forgive and never load-bearing here.
    if scope != ImpedimentScope.STOP:
        route_stop = None
    impediment = Impediment(
        route_id=route.id,
        route_stop_id=route_stop.id if route_stop is not None else None,
        scope=scope,
        reason=reason,
        note=note,
        reported_by=reported_by_id,
        reported_at=reported_at,
    )
    db.add(impediment)
    await db.flush()
    return impediment


async def list_pending(db: AsyncSession) -> list[Impediment]:
    """Return PENDING impediments oldest-first — the order the manager works the queue."""
    rows = await db.scalars(
        select(Impediment)
        .where(Impediment.status == ImpedimentStatus.PENDING)
        .order_by(Impediment.reported_at)
    )
    return list(rows.all())
