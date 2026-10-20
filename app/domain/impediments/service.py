# ruff: noqa: N818 — RouteNotInProgress / StopNotReassignable name the signal
# condition the caller catches by name, same convention as app/domain/forms/service.py.
"""Impediment reports service (RF16-RF17, spec 2026-10-20-alocacao-automatica §7.1): a field
worker's impediment gives the point(s) straight back to the pool and tells the managers; the
manager lists every impediment and acknowledges it ("ciente"). Ranking candidates and
reassigning by hand went with manual planning (§2.1).

Transaction discipline: these functions flush, never commit — the endpoint owns the
transaction boundary, same convention as app/domain/forms/service.py."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import Floor, ServicePoint
from app.domain.identity.models import User
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.monitoring.alerts_service import alert_rule_enabled
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.monitoring.notifications import describe_impediment, notify_impediment_reported
from app.domain.monitoring.push import PushSender
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from app.domain.routing.release import release_route, release_stops


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
    now: datetime,
    push: PushSender,
) -> Impediment:
    """Register a worker's impediment against a route (ROUTE_REMAINDER) or one of its
    stops (STOP), release at `now` what it covers — that stop, the worker keeping the rest of
    the batch; or every live stop, pausing the route — then raise the POINT_IMPEDED alert and
    notify the managers.

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
    released = await _release_impeded(db, route=route, route_stop=route_stop, now=now)
    if route_stop is not None and not released:
        # A check-out finished the stop after it was loaded: the row is DONE, nothing to impede.
        raise StopNotReassignable(f"Route stop {route_stop.id} was finished meanwhile.")
    description = await describe_impediment(
        db, route=route, route_stop=route_stop, reason=reason.value, released_count=len(released)
    )
    await _raise_point_impeded(db, impediment=impediment, description=description)
    # RF17: the managers learn about it in the notification center. The alert above is not
    # fanned out a second time.
    await notify_impediment_reported(
        db, impediment_id=impediment.id, route=route, description=description, push=push
    )
    return impediment


async def _release_impeded(
    db: AsyncSession, *, route: Route, route_stop: RouteStop | None, now: datetime
) -> list[RouteStop]:
    if route_stop is not None:
        return await release_stops(db, stops=[route_stop], reason=ReleaseReason.IMPEDIMENT, at=now)
    return await release_route(db, route=route, reason=ReleaseReason.ROUTE_REMAINDER, at=now)


async def _raise_point_impeded(
    db: AsyncSession, *, impediment: Impediment, description: str
) -> None:
    if not await alert_rule_enabled(db, AlertKind.POINT_IMPEDED):
        return
    db.add(
        Alert(
            kind=AlertKind.POINT_IMPEDED,
            severity=AlertSeverity.WARNING,
            dedup_key=f"POINT_IMPEDED:{impediment.id}",
            description=description,
            route_id=impediment.route_id,
            route_stop_id=impediment.route_stop_id,
            field_worker_id=impediment.reported_by,
        )
    )
    await db.flush()


# Everything the manager's list shows: the worker, and the point and building of a STOP report.
_LIST_OPTIONS = (
    selectinload(Impediment.reported_by_worker),
    selectinload(Impediment.route_stop)
    .selectinload(RouteStop.service_point)
    .selectinload(ServicePoint.floor)
    .selectinload(Floor.building),
)


async def find_impediments(
    db: AsyncSession, *, status: ImpedimentStatus | None
) -> list[Impediment]:
    """Every impediment (or those of `status`), PENDING first — what the manager still has to
    acknowledge — then newest first, with worker, point and building loaded.

        pending = await find_impediments(db, status=ImpedimentStatus.PENDING)
    """
    stmt = (
        select(Impediment)
        .options(*_LIST_OPTIONS)
        .order_by(
            case((Impediment.status == ImpedimentStatus.PENDING, 0), else_=1),
            Impediment.reported_at.desc(),
        )
    )
    if status is not None:
        stmt = stmt.where(Impediment.status == status)
    return list(await db.scalars(stmt))


async def get_impediment(db: AsyncSession, impediment_id: uuid.UUID) -> Impediment | None:
    """One impediment loaded like a row of `find_impediments`, or None."""
    impediment: Impediment | None = await db.scalar(
        select(Impediment)
        .where(Impediment.id == impediment_id)
        .options(*_LIST_OPTIONS)
        .execution_options(populate_existing=True)
    )
    return impediment


async def acknowledge_impediment(
    db: AsyncSession, *, impediment: Impediment, actor: User
) -> Impediment:
    """The manager's "ciente" (spec §8.4): PENDING → RESOLVED, stamped with who and when.
    Idempotent — an already RESOLVED impediment keeps its original stamp.

        impediment = await acknowledge_impediment(db, impediment=impediment, actor=manager)
    """
    if impediment.status is ImpedimentStatus.RESOLVED:
        return impediment
    impediment.status = ImpedimentStatus.RESOLVED
    impediment.resolved_by = actor.id
    impediment.resolved_at = datetime.now(UTC)
    await db.flush()
    return impediment
