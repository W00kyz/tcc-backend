# ruff: noqa: N818 — RouteNotInProgress / StopNotPending / AlreadyResolved name the signal
# condition the caller catches by name, same convention as app/domain/forms/service.py.
"""Impediment reports service: register a field worker's impediment, list the PENDING queue
for the manager, and dismiss one without triggering a reassignment (RF16-RF21, Etapa 8).

Transaction discipline: these functions flush, never commit — the endpoint owns the
transaction boundary, same convention as app/domain/forms/service.py."""

import uuid
from datetime import UTC, datetime

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


class ImpedimentNotFound(ImpedimentError):
    """No impediment exists with the given id."""


class RouteNotInProgress(ImpedimentError):
    """The route is not IN_PROGRESS, so no impediment can be reported against it."""


class StopNotPending(ImpedimentError):
    """A STOP-scope impediment targets a stop that is not PENDING."""


class AlreadyResolved(ImpedimentError):
    """The impediment was already dismissed or reassigned; it cannot be resolved again."""


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
    raises StopNotPending unless `route_stop.status == PENDING` — a ROUTE_REMAINDER report
    validates no individual stop; the route may have zero pending stops right now and the
    request is still recorded, since "nothing left to reassign" only matters at reassign
    time, not at report time."""
    if route.status != RouteStatus.IN_PROGRESS:
        raise RouteNotInProgress(
            f"Route {route.id} has status {route.status}; expected IN_PROGRESS."
        )
    if scope == ImpedimentScope.STOP and (
        route_stop is None or route_stop.status != RouteStopStatus.PENDING
    ):
        actual = route_stop.status if route_stop is not None else None
        raise StopNotPending(
            f"Route stop {route_stop and route_stop.id} has status {actual}; expected PENDING."
        )
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


async def dismiss_impediment(
    db: AsyncSession, *, impediment: Impediment, actor_id: uuid.UUID
) -> Impediment:
    """Mark an impediment RESOLVED without a reassignment. Raises AlreadyResolved if it
    is not currently PENDING."""
    if impediment.status == ImpedimentStatus.RESOLVED:
        raise AlreadyResolved(f"Impediment {impediment.id} is already RESOLVED; expected PENDING.")
    impediment.status = ImpedimentStatus.RESOLVED
    impediment.resolved_at = datetime.now(UTC)
    impediment.resolved_by = actor_id
    await db.flush()
    return impediment
