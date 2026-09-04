"""Route service layer (RF11-RF13) over the OSRM seam. Every mutating function `flush`es but
never `commit`s — the API layer (Task 4) owns the transaction and the `record_audit_trail`
call, mirroring `app/domain/execution/service.py`.

`stop_assignments` is append-only (spec §3.4, Ruling 1): a reassignment inserts `sequence + 1`
per pending stop and marks the prior row `REASSIGNED`, never editing sequence 1. A `DONE` stop
is never removed or moved (spec §5.3, Ruling 9). Any write recomputes every leg (spec §4.1,
Ruling 5); an un-routable leg or an OSRM outage degrades to `NULL` legs, it never blocks the
write (Ruling 6).

Example:
    route = await create_route(
        db, field_worker_id=w.id, route_date=date(2026, 9, 1), route_type=RouteType.REGULAR,
        scheduled_start_at=None, stops=[StopInput(service_point_id=p.id)], actor_id=manager.id,
        osrm=osrm_client,
    )
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker, ServicePoint
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    RouteType,
    StopAssignment,
    StopAssignmentOutcome,
)
from app.domain.routing.osrm import OsrmClient, OsrmUnavailable
from app.domain.routing.templates import RouteTemplate


@dataclass(frozen=True)
class StopInput:
    service_point_id: uuid.UUID
    expected_arrival_from: datetime | None = None
    expected_arrival_to: datetime | None = None
    service_type_id: uuid.UUID | None = None


class RouteNotEditable(Exception):
    """The route is CANCELLED or DONE — no stop edit, optimise, reassign or cancel is allowed."""


class DoneStopRemoved(Exception):
    """A PATCH stop list omitted a stop that is already DONE (spec §5.3, Ruling 9)."""


class UnknownServicePoint(Exception):
    """A StopInput references a service_point_id that does not exist."""


class UnknownFieldWorker(Exception):
    """A route write references a field_worker_id with no field_workers row (would surface as a
    FK IntegrityError at commit — the API layer maps this to 404 instead)."""


class TemplateHasNoWorker(Exception):
    """`materialize_template` got neither a body `field_worker_id` nor a template default —
    there is no worker to assign the materialised route to (spec §5.1; the API maps this to
    422)."""


async def ensure_field_worker_exists(db: AsyncSession, field_worker_id: uuid.UUID) -> None:
    if await db.get(FieldWorker, field_worker_id) is None:
        raise UnknownFieldWorker(
            f'Unknown field worker "{field_worker_id}"; a route must be assigned to an '
            f"existing field_workers row."
        )


async def create_route(
    db: AsyncSession,
    *,
    field_worker_id: uuid.UUID,
    route_date: date,
    route_type: RouteType,
    scheduled_start_at: datetime | None,
    stops: list[StopInput],
    actor_id: uuid.UUID,
    osrm: OsrmClient,
) -> Route:
    """Create a PLANNED route, its ordered stops, a sequence-1 assignment per stop, and the
    OSRM legs. The array position is the `order_index`. Raises `UnknownFieldWorker`,
    `UnknownServicePoint`."""
    await ensure_field_worker_exists(db, field_worker_id)
    await ensure_service_points_exist(db, [item.service_point_id for item in stops])

    route = Route(
        field_worker_id=field_worker_id,
        route_date=route_date,
        route_type=route_type,
        scheduled_start_at=scheduled_start_at,
        status=RouteStatus.PLANNED,
    )
    db.add(route)
    await db.flush()

    for index, item in enumerate(stops, start=1):
        stop = RouteStop(
            route_id=route.id,
            service_point_id=item.service_point_id,
            order_index=index,
            expected_arrival_from=item.expected_arrival_from,
            expected_arrival_to=item.expected_arrival_to,
            service_type_id=item.service_type_id,
        )
        db.add(stop)
        await db.flush()
        db.add(
            StopAssignment(
                route_stop_id=stop.id,
                field_worker_id=field_worker_id,
                sequence=1,
                assigned_by=actor_id,
            )
        )

    await db.flush()
    await _recompute_legs(db, route=route, osrm=osrm)
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


async def materialize_template(
    db: AsyncSession,
    *,
    template: RouteTemplate,
    route_date: date,
    field_worker_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    osrm: OsrmClient,
) -> Route:
    """Turn a `RouteTemplate` into a concrete `Route` for `route_date` (spec §3.3, §5.2
    Ruling 8 — this is the manual step; no scheduler calls it yet). Each template stop's
    time-of-day is combined with `route_date` into a UTC timestamp for the route stop. The
    worker is the body's `field_worker_id` if given, else the template's default; if both are
    None, raises `TemplateHasNoWorker`. Also raises `UnknownFieldWorker`, `UnknownServicePoint`.

        route = await materialize_template(
            db, template=tmpl, route_date=date(2026, 9, 7),
            field_worker_id=None, actor_id=manager.id, osrm=osrm_client,
        )
    """
    worker_id = field_worker_id or template.field_worker_id
    if worker_id is None:
        raise TemplateHasNoWorker(
            f'Template "{template.id}" fixes no field worker and the request supplied none; '
            f"a materialised route must be assigned to a field worker."
        )
    stops = [
        StopInput(
            service_point_id=stop.service_point_id,
            expected_arrival_from=_combine_date_time(route_date, stop.expected_arrival_from),
            expected_arrival_to=_combine_date_time(route_date, stop.expected_arrival_to),
        )
        for stop in template.stops
    ]
    route = await create_route(
        db,
        field_worker_id=worker_id,
        route_date=route_date,
        route_type=template.route_type,
        scheduled_start_at=None,
        stops=stops,
        actor_id=actor_id,
        osrm=osrm,
    )
    route.template_id = template.id
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


def _combine_date_time(route_date: date, value: time | None) -> datetime | None:
    """A template stop stores a bare time-of-day (it has no date); materialising fixes it to
    `route_date` in UTC (spec §3.3)."""
    if value is None:
        return None
    return datetime.combine(route_date, value, tzinfo=UTC)


async def replace_route_stops(
    db: AsyncSession,
    *,
    route: Route,
    stops: list[StopInput],
    actor_id: uuid.UUID,
    osrm: OsrmClient,
) -> Route:
    """Replace the whole PENDING stop list — create, delete or reorder pending stops — keeping
    every DONE stop pinned at the front in its original relative order (spec §5.3). A stop that
    is genuinely new gets a sequence-1 `StopAssignment` with `assigned_by = actor_id` — the
    manager running this PATCH is the one making the designation (spec §3.4, Ruling 1). Raises
    `RouteNotEditable`, `DoneStopRemoved`, `UnknownServicePoint`."""
    _ensure_editable(route)
    current = await _ordered_stops(db, route)
    done_stops = [s for s in current if s.status == RouteStopStatus.DONE]
    pending_by_point = {
        s.service_point_id: s for s in current if s.status == RouteStopStatus.PENDING
    }
    done_point_ids = {s.service_point_id for s in done_stops}
    new_point_ids = [item.service_point_id for item in stops]

    missing_done = done_point_ids - set(new_point_ids)
    if missing_done:
        raise DoneStopRemoved(
            f"Route stop(s) for service point(s) {sorted(str(m) for m in missing_done)} are "
            f'already done and cannot be removed from route "{route.id}".'
        )
    await ensure_service_points_exist(db, new_point_ids)

    for point_id, stop in pending_by_point.items():
        if point_id not in new_point_ids:
            await _delete_stop(db, stop)
    await db.flush()

    done_stops.sort(key=lambda s: s.order_index)
    for index, stop in enumerate(done_stops, start=1):
        stop.order_index = index
    next_index = len(done_stops) + 1

    for item in stops:
        if item.service_point_id in done_point_ids:
            continue  # DONE stops are pinned — their position in the new list is ignored.
        existing = pending_by_point.get(item.service_point_id)
        if existing is not None:
            existing.order_index = next_index
            existing.expected_arrival_from = item.expected_arrival_from
            existing.expected_arrival_to = item.expected_arrival_to
            existing.service_type_id = item.service_type_id
            # Setting the FK column directly (there is no `ServiceType` object here to assign
            # through the relationship) leaves `RouteStop.service_type` cached from before this
            # update — expire it so the reload after this PATCH re-selects it and
            # `RouteStopOut.service_type_name` reflects the new type, not the old one.
            db.expire(existing, ["service_type"])
        else:
            new_stop = RouteStop(
                route_id=route.id,
                service_point_id=item.service_point_id,
                order_index=next_index,
                expected_arrival_from=item.expected_arrival_from,
                expected_arrival_to=item.expected_arrival_to,
                service_type_id=item.service_type_id,
            )
            db.add(new_stop)
            await db.flush()
            db.add(
                StopAssignment(
                    route_stop_id=new_stop.id,
                    field_worker_id=route.field_worker_id,
                    sequence=1,
                    assigned_by=actor_id,
                )
            )
        next_index += 1

    await db.flush()
    await _recompute_legs(db, route=route, osrm=osrm)
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


async def optimize_route(db: AsyncSession, *, route: Route, osrm: OsrmClient) -> Route:
    """Ask OSRM's Trip service for the best visiting order of the PENDING stops, reindex them
    after the DONE stops, and recompute legs. Propagates `OsrmUnavailable` (spec §4.2 — the
    optimise endpoint answers 503, unlike a plain save). Raises `RouteNotEditable`."""
    _ensure_editable(route)
    stops = await _ordered_stops(db, route)
    done_stops = [s for s in stops if s.status == RouteStopStatus.DONE]
    pending_stops = [s for s in stops if s.status == RouteStopStatus.PENDING]

    if len(pending_stops) >= 2:
        points = await _points_by_id(db, [s.service_point_id for s in pending_stops])
        coordinates = [
            (points[s.service_point_id].longitude, points[s.service_point_id].latitude)
            for s in pending_stops
        ]
        order = await osrm.optimize_order(coordinates)
        pending_stops = [pending_stops[i] for i in order]

    done_stops.sort(key=lambda s: s.order_index)
    for index, stop in enumerate(done_stops, start=1):
        stop.order_index = index
    for index, stop in enumerate(pending_stops, start=len(done_stops) + 1):
        stop.order_index = index

    await db.flush()
    await _recompute_legs(db, route=route, osrm=osrm)
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


async def reassign_route(
    db: AsyncSession,
    *,
    route: Route,
    new_field_worker_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID,
) -> Route:
    """Append-only reassignment (spec §3.4). For each PENDING stop: mark the current top-of-
    chain assignment `REASSIGNED` and insert `sequence + 1` for the new worker. DONE stops are
    left untouched. Raises `RouteNotEditable`, `UnknownFieldWorker`."""
    _ensure_editable(route)
    await ensure_field_worker_exists(db, new_field_worker_id)
    for stop in await _ordered_stops(db, route):
        if stop.status != RouteStopStatus.PENDING:
            continue
        assignments = await _assignments_for(db, stop)
        if not assignments:
            continue
        current = assignments[-1]
        current.outcome = StopAssignmentOutcome.REASSIGNED
        db.add(
            StopAssignment(
                route_stop_id=stop.id,
                field_worker_id=new_field_worker_id,
                sequence=current.sequence + 1,
                assigned_by=actor_id,
                transfer_reason=reason,
            )
        )

    route.field_worker_id = new_field_worker_id
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


async def cancel_route(db: AsyncSession, *, route: Route, reason: str) -> Route:
    """Set `status = CANCELLED` and the reason, and mark every still-open assignment on a
    PENDING stop `CANCELLED`. Raises `RouteNotEditable` if already CANCELLED or DONE."""
    _ensure_editable(route)
    route.status = RouteStatus.CANCELLED
    route.cancellation_reason = reason
    for stop in await _ordered_stops(db, route):
        if stop.status != RouteStopStatus.PENDING:
            continue
        for assignment in await _assignments_for(db, stop):
            if assignment.outcome is None:
                assignment.outcome = StopAssignmentOutcome.CANCELLED

    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


def routing_degraded(route: Route) -> bool:
    """True when any PENDING stop after the first has no leg geometry — the single, uniform
    "OSRM could not draw this leg" signal (spec §4.2, Ruling 6). `route.stops` must be loaded;
    every service function here leaves it loaded and ordered."""
    ordered = sorted(route.stops, key=lambda s: s.order_index)
    return any(
        stop.leg_geometry is None for stop in ordered[1:] if stop.status == RouteStopStatus.PENDING
    )


async def _recompute_legs(db: AsyncSession, *, route: Route, osrm: OsrmClient) -> bool:
    """Rewrite `distance_from_prev_m` / `duration_from_prev_s` / `leg_geometry` on every stop
    in the current order. `stops[0]` always gets `None` (no previous stop). A zeroed leg or an
    OSRM outage stores `None` in all three columns, so `leg_geometry is None` is the uniform
    degraded signal. Returns the degraded flag."""
    await db.flush()
    stops = await _ordered_stops(db, route)
    for stop in stops:
        _clear_leg(stop)
    if len(stops) < 2:
        await db.flush()
        return False

    points = await _points_by_id(db, [s.service_point_id for s in stops])
    coordinates = [
        (points[s.service_point_id].longitude, points[s.service_point_id].latitude) for s in stops
    ]
    try:
        legs = await osrm.route_legs(coordinates)
    except OsrmUnavailable:
        await db.flush()
        return True

    for index, stop in enumerate(stops[1:], start=1):
        leg = legs[index - 1]
        if not leg.geometry:
            continue  # zeroed leg — leave the three columns None (Ruling 6).
        stop.distance_from_prev_m = leg.distance_m
        stop.duration_from_prev_s = leg.duration_s
        stop.leg_geometry = leg.geometry

    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return routing_degraded(route)


def _clear_leg(stop: RouteStop) -> None:
    stop.distance_from_prev_m = None
    stop.duration_from_prev_s = None
    stop.leg_geometry = None


def _ensure_editable(route: Route) -> None:
    if route.status in (RouteStatus.CANCELLED, RouteStatus.DONE):
        raise RouteNotEditable(f'Route "{route.id}" is {route.status.value} and cannot be edited.')


async def ensure_service_points_exist(db: AsyncSession, service_point_ids: list[uuid.UUID]) -> None:
    unique_ids = set(service_point_ids)
    if not unique_ids:
        return
    found = set(
        (await db.scalars(select(ServicePoint.id).where(ServicePoint.id.in_(unique_ids)))).all()
    )
    missing = unique_ids - found
    if missing:
        raise UnknownServicePoint(
            f"Unknown service point(s): {sorted(str(m) for m in missing)}; "
            f"every stop must reference an existing service_points row."
        )


async def _ordered_stops(db: AsyncSession, route: Route) -> list[RouteStop]:
    result = await db.scalars(
        select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.order_index)
    )
    return list(result)


async def _assignments_for(db: AsyncSession, stop: RouteStop) -> list[StopAssignment]:
    result = await db.scalars(
        select(StopAssignment)
        .where(StopAssignment.route_stop_id == stop.id)
        .order_by(StopAssignment.sequence)
    )
    return list(result)


async def _points_by_id(
    db: AsyncSession, service_point_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ServicePoint]:
    result = await db.scalars(
        select(ServicePoint).where(ServicePoint.id.in_(set(service_point_ids)))
    )
    return {point.id: point for point in result}


async def _delete_stop(db: AsyncSession, stop: RouteStop) -> None:
    await db.execute(delete(StopAssignment).where(StopAssignment.route_stop_id == stop.id))
    await db.delete(stop)
