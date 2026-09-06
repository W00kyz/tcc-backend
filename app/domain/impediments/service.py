# ruff: noqa: N818 — RouteNotInProgress / StopNotReassignable / AlreadyResolved name the signal
# condition the caller catches by name, same convention as app/domain/forms/service.py.
"""Impediment reports service: register a field worker's impediment, list the PENDING queue
for the manager, rank reassignment candidates by PostGIS distance and current load, and
reassign the affected stop(s) to a chosen field worker (RF16-RF21, Etapa 8).

Transaction discipline: these functions flush, never commit — the endpoint owns the
transaction boundary, same convention as app/domain/forms/service.py."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import FieldWorker, FieldWorkerServiceType, ServicePoint
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from app.domain.routing.osrm import OsrmClient
from app.domain.routing.service import reassign_stops


class ImpedimentError(Exception):
    """Base class for every impediment-service failure."""


class ImpedimentNotFound(ImpedimentError):
    """No impediment exists with the given id."""


class RouteNotInProgress(ImpedimentError):
    """The route is not IN_PROGRESS, so no impediment can be reported against it."""


class StopNotReassignable(ImpedimentError):
    """A STOP-scope impediment targets a stop that can no longer move — it is neither PENDING
    (never reached) nor IN_PROGRESS (checked in, stuck); a DONE stop is finished work."""


class AlreadyResolved(ImpedimentError):
    """The impediment was already dismissed or reassigned; it cannot be resolved again."""


class NoAffectedStops(ImpedimentError):
    """A ROUTE_REMAINDER impediment's route has no PENDING stop left to reassign."""


class NoRouteForDate(ImpedimentError):
    """`new_field_worker_id` has no PLANNED/IN_PROGRESS route on the impeded route's date, so
    there is nowhere to move the affected stop(s) to."""


class SameRouteReassignment(ImpedimentError):
    """`new_field_worker_id`'s current route on that date IS the impeded route itself. Task 3's
    review flagged this: `reassign_stops` would accept source_route_id == target_route_id
    without corrupting data (no gaps/duplicates), but RF18 means handing the stop(s) to a
    DIFFERENT professional — silently accepting this would be a semantic no-op disguised as a
    reassignment, so it is rejected here before `reassign_stops` is ever called."""


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
    stops right now and the request is still recorded, since "nothing left to reassign"
    only matters at reassign time, not at report time."""
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


@dataclass(frozen=True)
class CandidateOut:
    field_worker_id: uuid.UUID
    full_name: str
    distance_m: float
    pending_stop_count: int


async def candidates_for(db: AsyncSession, *, impediment: Impediment) -> list[CandidateOut]:
    """Rank the field workers eligible to take over an impediment's affected stop(s) (spec
    Ruling 3): a candidate must have a PLANNED/IN_PROGRESS route on the SAME `route_date` as
    the impeded route (excluding that route itself), a linked `user_id` (RF08 — no app
    access, no reassignment target), and be qualified for EVERY service type among the
    affected stops. Ordered by `distance_m` ascending (closest current pending stop to any
    affected point), ties broken by `pending_stop_count` ascending (spec Ruling 3 — prefer the
    lighter-loaded candidate)."""
    route = await db.get(Route, impediment.route_id)
    if route is None:
        return []
    affected_stops = await _affected_stops(db, impediment)
    if not affected_stops:
        return []
    required_type_ids = {s.service_type_id for s in affected_stops if s.service_type_id}
    target_points = await _points_by_id_local(db, [s.service_point_id for s in affected_stops])

    candidate_routes = (
        await db.scalars(
            select(Route)
            .where(
                Route.route_date == route.route_date,
                Route.status.in_([RouteStatus.PLANNED, RouteStatus.IN_PROGRESS]),
                Route.id != route.id,
            )
            .options(selectinload(Route.stops).selectinload(RouteStop.service_point))
        )
    ).all()

    results: list[CandidateOut] = []
    for candidate_route in candidate_routes:
        worker = await db.get(FieldWorker, candidate_route.field_worker_id)
        if worker is None or worker.user_id is None:
            continue
        if required_type_ids:
            worker_type_ids = set(
                (
                    await db.scalars(
                        select(FieldWorkerServiceType.service_type_id).where(
                            FieldWorkerServiceType.field_worker_id == worker.id
                        )
                    )
                ).all()
            )
            if not required_type_ids.issubset(worker_type_ids):
                continue
        open_stops = [
            s
            for s in candidate_route.stops
            if s.status in (RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS)
        ]
        if not open_stops:
            continue
        # PERF: one ST_DistanceSphere query per (candidate pending stop, affected point) pair —
        # acceptable for the real volume (few field workers, few points per impediment). A
        # future version could compose this into a single query with unnest/subquery if the
        # volume grows; not worth the complexity now (YAGNI).
        best: float | None = None
        for cand_stop in open_stops:
            cp = cand_stop.service_point
            for target_point in target_points.values():
                dist = await db.scalar(
                    select(
                        func.ST_DistanceSphere(
                            func.ST_MakePoint(cp.longitude, cp.latitude),
                            func.ST_MakePoint(target_point.longitude, target_point.latitude),
                        )
                    )
                )
                if dist is not None and (best is None or dist < best):
                    best = dist
        if best is None:
            continue
        results.append(
            CandidateOut(
                field_worker_id=worker.id,
                full_name=worker.full_name,
                distance_m=float(best),
                pending_stop_count=len(open_stops),
            )
        )
    results.sort(key=lambda c: (c.distance_m, c.pending_stop_count))
    return results


async def reassign_impediment(
    db: AsyncSession,
    *,
    impediment: Impediment,
    new_field_worker_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID,
    osrm: OsrmClient,
) -> Impediment:
    """Move the impediment's affected stop(s) — the one STOP stop, or every PENDING stop of
    the route for ROUTE_REMAINDER — onto `new_field_worker_id`'s current route for the same
    `route_date`, delegating the actual move to `reassign_stops` (Task 3, spec Ruling 1), then
    mark the impediment RESOLVED. `reassign_stops` is all-or-nothing (a single bad stop raises
    before any stop is mutated), so this call carries that same guarantee for the whole batch.

    Raises AlreadyResolved if already resolved; NoAffectedStops if scope=ROUTE_REMAINDER and
    the route has no PENDING stop left; NoRouteForDate if `new_field_worker_id` has no
    PLANNED/IN_PROGRESS route on that date; SameRouteReassignment if that route IS the impeded
    route itself. Also propagates `reassign_stops`' own StopNotOnRoute, StopNotReassignable,
    RouteNotEditable and UnknownFieldWorker."""
    if impediment.status == ImpedimentStatus.RESOLVED:
        raise AlreadyResolved(f"Impediment {impediment.id} is already RESOLVED; expected PENDING.")
    route = await db.get(Route, impediment.route_id)
    if route is None:
        raise NoAffectedStops(f'Impediment "{impediment.id}" references no existing route.')
    affected_stops = await _affected_stops(db, impediment)
    if not affected_stops:
        raise NoAffectedStops(
            f'Route "{route.id}" has no PENDING stop left to reassign for '
            f'impediment "{impediment.id}".'
        )

    target_route = (
        await db.scalars(
            select(Route).where(
                Route.field_worker_id == new_field_worker_id,
                Route.route_date == route.route_date,
                Route.status.in_([RouteStatus.PLANNED, RouteStatus.IN_PROGRESS]),
            )
        )
    ).first()
    if target_route is None:
        raise NoRouteForDate(
            f'Field worker "{new_field_worker_id}" has no PLANNED/IN_PROGRESS route on '
            f"{route.route_date}; expected an existing route to reassign the stop(s) onto."
        )
    if target_route.id == route.id:
        raise SameRouteReassignment(
            f'Field worker "{new_field_worker_id}" is already assigned to route "{route.id}"; '
            f"expected a different professional's route (RF18)."
        )

    await reassign_stops(
        db,
        source_route=route,
        target_route=target_route,
        stop_ids=[stop.id for stop in affected_stops],
        new_field_worker_id=new_field_worker_id,
        reason=reason,
        actor_id=actor_id,
        osrm=osrm,
    )

    impediment.status = ImpedimentStatus.RESOLVED
    impediment.resolved_at = datetime.now(UTC)
    impediment.resolved_by = actor_id
    await db.flush()
    return impediment


async def _affected_stops(db: AsyncSession, impediment: Impediment) -> list[RouteStop]:
    """The stop(s) an impediment covers: the one route_stop for STOP, or every still-open
    (PENDING or IN_PROGRESS) stop of the impeded route for ROUTE_REMAINDER (ordered — same
    order `reassign_stops` re-indexes the target route in)."""
    if impediment.scope == ImpedimentScope.STOP:
        stop = await db.get(RouteStop, impediment.route_stop_id)
        return [stop] if stop is not None else []
    stops = await db.scalars(
        select(RouteStop)
        .where(
            RouteStop.route_id == impediment.route_id,
            RouteStop.status.in_([RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS]),
        )
        .order_by(RouteStop.order_index)
    )
    return list(stops)


async def _points_by_id_local(
    db: AsyncSession, service_point_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ServicePoint]:
    result = await db.scalars(
        select(ServicePoint).where(ServicePoint.id.in_(set(service_point_ids)))
    )
    return {point.id: point for point in result}
