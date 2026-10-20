# ruff: noqa: N818 — NotAServiceDay / WorkdayOver / ... name the refusal the caller catches by
# name, same convention as app/domain/impediments/service.py.
"""The automatic allocation (spec 2026-10-20-alocacao-automatica §6). A field worker pulls small
batches of the nearest free service points of their company; a point is live on at most one stop
per day, which the database enforces (`uq_route_stops_live_point_day`).

Transaction discipline: these functions flush, never commit. The endpoint commits right after,
which is what releases the row locks the claim takes.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, exists, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.campus_clock import campus_today
from app.domain.catalog.models import FieldWorker, Floor, ServicePoint, ServiceType
from app.domain.execution.geo import haversine_meters
from app.domain.impediments.models import Impediment
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from app.domain.routing.osrm import OsrmClient, OsrmUnavailable
from app.domain.routing.service import recompute_legs
from app.domain.settings.campus import inside_campus, is_service_day, workday_over
from app.domain.settings.models import SystemSettings

# The statuses that hold a point for the day — the predicate of the exclusivity index.
_TAKEN_STATUSES = (RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS, RouteStopStatus.DONE)
# The statuses of work still to do on the current batch.
_OPEN_STATUSES = (RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS)
_EXCLUSIVITY_INDEX = "uq_route_stops_live_point_day"


class AllocationRefused(Exception):
    """Base class for every reason no batch is handed out."""


class NotAServiceDay(AllocationRefused):
    """The campus day is not one of the service weekdays."""


class WorkdayOver(AllocationRefused):
    """The campus wall clock has reached the end of the workday."""


class OutsideCampus(AllocationRefused):
    """The worker's position is outside the campus rectangle."""


class RouteAlreadyDone(AllocationRefused):
    """The route is closed for the day — the pool ran out, or the day is over."""


@dataclass
class AllocationResult:
    route: Route
    # True when the worker asked for more and the pool had nothing left: the route is now DONE.
    pool_empty: bool


async def start_or_resume(
    db: AsyncSession,
    *,
    worker: FieldWorker,
    today: date,
    now: datetime,
    lat: float,
    lng: float,
    settings: SystemSettings,
    osrm: OsrmClient | None,
) -> AllocationResult:
    """The "Iniciar rota" button: create or reopen the worker's route of `today` and hand out a
    batch. A retry while the batch is open returns it unchanged. Raises an `AllocationRefused`
    before any route row is created or changed.

        result = await start_or_resume(db, worker=w, today=d, now=n, lat=la, lng=lo,
                                       settings=s, osrm=osrm)
    """
    _ensure_allocatable(settings, today=today, now=now, lat=lat, lng=lng)
    route = await _lock_day_route(db, worker=worker, today=today)
    if route.started_at is None:
        route.started_at = now
        route.start_latitude = lat
        route.start_longitude = lng
    return await _advance(db, route=route, now=now, origin=(lat, lng), settings=settings, osrm=osrm)


async def next_batch(
    db: AsyncSession,
    *,
    route: Route,
    now: datetime,
    lat: float,
    lng: float,
    settings: SystemSettings,
    osrm: OsrmClient | None,
) -> AllocationResult:
    """The next batch from the worker's current position, once the current one has no open stop
    (while it has one, the route comes back unchanged). An empty pool closes the route (`DONE`,
    `pool_empty=True`). Same refusals as `start_or_resume`.

        result = await next_batch(db, route=route, now=n, lat=la, lng=lo, settings=s, osrm=osrm)
    """
    today = campus_today(now)
    _ensure_allocatable(settings, today=today, now=now, lat=lat, lng=lng)
    if route.route_date != today:
        raise RouteAlreadyDone(f"Route {route.id} is for {route.route_date}; today is {today}.")
    route = await _lock_route(db, Route.id == route.id)
    return await _advance(db, route=route, now=now, origin=(lat, lng), settings=settings, osrm=osrm)


async def free_points(
    db: AsyncSession, *, company_id: uuid.UUID, day: date, exclude_point_ids: set[uuid.UUID]
) -> list[ServicePoint]:
    """The pool (spec §6.1): points whose service type belongs to `company_id` and that no stop
    of `day` holds. A point without a type, or whose type has no company, is never in it. Comes
    back with floor and building loaded, for the batch ordering.

        pool = await free_points(db, company_id=company.id, day=today, exclude_point_ids=set())
    """
    taken = exists().where(
        RouteStop.service_point_id == ServicePoint.id,
        RouteStop.service_date == day,
        RouteStop.status.in_(_TAKEN_STATUSES),
    )
    stmt = (
        select(ServicePoint)
        .join(ServiceType, ServicePoint.service_type_id == ServiceType.id)
        .where(ServiceType.contractor_company_id == company_id, ~taken)
        .options(selectinload(ServicePoint.floor).selectinload(Floor.building))
    )
    if exclude_point_ids:
        stmt = stmt.where(ServicePoint.id.not_in(exclude_point_ids))
    return list(await db.scalars(stmt))


# --- guards and the route row ----------------------------------------------------------------


def _ensure_allocatable(
    settings: SystemSettings, *, today: date, now: datetime, lat: float, lng: float
) -> None:
    if not is_service_day(settings, today):
        raise NotAServiceDay(
            f"{today} (weekday {today.weekday()}) is not a service day; "
            f"service weekdays are {settings.service_weekdays}."
        )
    if workday_over(settings, now):
        raise WorkdayOver(f"The workday ended at {settings.workday_end_time} campus time.")
    if not inside_campus(settings, lat, lng):
        raise OutsideCampus(
            f"Position ({lat}, {lng}) is outside the campus "
            f"[{settings.campus_min_lat}, {settings.campus_max_lat}] x "
            f"[{settings.campus_min_lng}, {settings.campus_max_lng}]."
        )


async def _lock_day_route(db: AsyncSession, *, worker: FieldWorker, today: date) -> Route:
    """The worker's route of `today`, created if missing, row-locked until commit. ON CONFLICT
    makes two simultaneous first taps of the same worker share one route instead of one of them
    failing on the one-route-per-day constraint; the lock then serializes them."""
    await db.execute(
        pg_insert(Route)
        .values(field_worker_id=worker.id, route_date=today, status=RouteStatus.IN_PROGRESS)
        .on_conflict_do_nothing(constraint="uq_routes_field_worker_id_route_date")
    )
    return await _lock_route(db, Route.field_worker_id == worker.id, Route.route_date == today)


async def _lock_route(db: AsyncSession, *criteria: ColumnElement[bool]) -> Route:
    stmt = (
        select(Route).where(*criteria).with_for_update().execution_options(populate_existing=True)
    )
    return (await db.scalars(stmt)).one()


# --- the batch -------------------------------------------------------------------------------


async def _advance(
    db: AsyncSession,
    *,
    route: Route,
    now: datetime,
    origin: tuple[float, float],
    settings: SystemSettings,
    osrm: OsrmClient | None,
) -> AllocationResult:
    if await _has_open_stop(db, route):  # an idempotent retry: the batch is still being worked
        await db.refresh(route, attribute_names=["stops"])
        return AllocationResult(route=route, pool_empty=False)
    if route.status is RouteStatus.DONE:
        raise RouteAlreadyDone(f"Route {route.id} of {route.route_date} is already DONE.")
    claimed = await _claim(
        db,
        route=route,
        company_id=await _company_of(db, route),
        origin=origin,
        size=settings.allocation_batch_size,
        exclude=await _impeded_by_worker(db, route),
    )
    if not claimed:
        route.status = RouteStatus.DONE
        await db.flush()
        await db.refresh(route, attribute_names=["stops"])
        return AllocationResult(route=route, pool_empty=True)
    route.status = RouteStatus.IN_PROGRESS
    route.last_allocated_at = now
    await _order_batch(claimed, origin=origin, osrm=osrm)
    await _refresh_legs(db, route, osrm)
    return AllocationResult(route=route, pool_empty=False)


async def _has_open_stop(db: AsyncSession, route: Route) -> bool:
    # Any open stop of the route is in its latest batch: a batch is only handed out once the
    # previous one has none left.
    return bool(
        await db.scalar(
            select(
                exists().where(RouteStop.route_id == route.id, RouteStop.status.in_(_OPEN_STATUSES))
            )
        )
    )


async def _company_of(db: AsyncSession, route: Route) -> uuid.UUID:
    result = await db.execute(
        select(FieldWorker.contractor_company_id).where(FieldWorker.id == route.field_worker_id)
    )
    return result.scalar_one()


async def _impeded_by_worker(db: AsyncSession, route: Route) -> set[uuid.UUID]:
    """Points the route's worker reported an impediment on that day: they go back to the pool
    for everybody else, not for them (spec §6.1)."""
    rows = await db.scalars(
        select(RouteStop.service_point_id)
        .join(Impediment, Impediment.route_stop_id == RouteStop.id)
        .where(
            Impediment.reported_by == route.field_worker_id,
            RouteStop.service_date == route.route_date,
        )
    )
    return set(rows)


async def _claim(
    db: AsyncSession,
    *,
    route: Route,
    company_id: uuid.UUID,
    origin: tuple[float, float],
    size: int,
    exclude: set[uuid.UUID],
) -> list[RouteStop]:
    """Take up to `size` of the nearest free points as PENDING stops of a new batch (spec §6.3).

    The candidates are row-locked with SKIP LOCKED, so a concurrent claim skips the points this
    one is taking instead of waiting on them. The exclusivity index is the last barrier: a point
    a concurrent claim committed after the pool was read is refused by it and skipped.
    """
    pool = await free_points(
        db, company_id=company_id, day=route.route_date, exclude_point_ids=exclude
    )
    candidates = _nearest_first(pool, origin)
    batch_number, first_index = await _next_batch_slot(db, route)
    tried: set[uuid.UUID] = set()
    claimed: list[RouteStop] = []
    while len(claimed) < size:
        untried = [point for point in candidates if point.id not in tried]
        locked = await _lock_points(db, [point.id for point in untried], limit=size - len(claimed))
        if not locked:
            return claimed
        for point in (point for point in untried if point.id in locked):
            tried.add(point.id)
            stop = _new_stop(route, point, batch_number, order_index=first_index + len(claimed))
            if await _insert_stop(db, stop):
                claimed.append(stop)
    return claimed


def _nearest_first(points: list[ServicePoint], origin: tuple[float, float]) -> list[ServicePoint]:
    # Points of one building share its centre, so the ties are broken by building, floor and
    # name — stable, and what spreads a big building over the workers who arrive together.
    return sorted(
        points,
        key=lambda p: (
            haversine_meters(origin[0], origin[1], p.latitude, p.longitude),
            p.floor.building.name,
            p.floor.label,
            p.name,
        ),
    )


async def _next_batch_slot(db: AsyncSession, route: Route) -> tuple[int, int]:
    """The new batch's number and the `order_index` its first stop takes."""
    last_batch, last_index = (
        await db.execute(
            select(func.max(RouteStop.batch_number), func.max(RouteStop.order_index)).where(
                RouteStop.route_id == route.id
            )
        )
    ).one()
    return (last_batch or 0) + 1, (last_index or 0) + 1


async def _lock_points(db: AsyncSession, ids: list[uuid.UUID], *, limit: int) -> set[uuid.UUID]:
    """Row-lock up to `limit` of `ids`, nearest first (the order of `ids`), skipping the rows a
    concurrent claim holds. LIMIT applies after the locking, so a skipped row does not count."""
    if not ids:
        return set()
    stmt = (
        select(ServicePoint.id)
        .where(ServicePoint.id.in_(ids))
        .order_by(func.array_position(literal(ids, ARRAY(UUID(as_uuid=True))), ServicePoint.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return set(await db.scalars(stmt))


def _new_stop(
    route: Route, point: ServicePoint, batch_number: int, *, order_index: int
) -> RouteStop:
    return RouteStop(
        route_id=route.id,
        service_point=point,
        service_type_id=point.service_type_id,
        order_index=order_index,
        batch_number=batch_number,
        service_date=route.route_date,
        status=RouteStopStatus.PENDING,
    )


async def _insert_stop(db: AsyncSession, stop: RouteStop) -> bool:
    """Insert `stop` inside a SAVEPOINT; False when the exclusivity index refuses it — someone
    else's stop holds the point — leaving the rest of the transaction intact."""
    try:
        async with db.begin_nested():
            db.add(stop)
    except IntegrityError as exc:
        if _EXCLUSIVITY_INDEX not in str(exc.orig):
            raise
        return False
    return True


async def _order_batch(
    stops: list[RouteStop], *, origin: tuple[float, float], osrm: OsrmClient | None
) -> None:
    """Rewrite the batch's `order_index`: buildings in OSRM trip order from the worker (the
    distance order when OSRM is unavailable), then by floor and name inside a building."""
    by_building: dict[uuid.UUID, list[RouteStop]] = {}
    for stop in stops:  # in claim (distance) order — the fallback building order
        by_building.setdefault(stop.service_point.floor.building_id, []).append(stop)
    buildings = list(by_building.values())
    first_index = min(stop.order_index for stop in stops)
    ordered = [
        stop
        for position in await _building_order(buildings, origin=origin, osrm=osrm)
        for stop in sorted(
            buildings[position], key=lambda s: (s.service_point.floor.label, s.service_point.name)
        )
    ]
    for offset, stop in enumerate(ordered):
        stop.order_index = first_index + offset


async def _building_order(
    buildings: list[list[RouteStop]], *, origin: tuple[float, float], osrm: OsrmClient | None
) -> list[int]:
    """Positions into `buildings` in OSRM trip order, starting from the worker (waypoint 0)."""
    fallback = list(range(len(buildings)))
    if osrm is None or len(buildings) < 2:
        return fallback
    coordinates = [(origin[1], origin[0])] + [
        (stops[0].service_point.longitude, stops[0].service_point.latitude) for stops in buildings
    ]
    try:
        order = await osrm.optimize_order(coordinates)
    except OsrmUnavailable:
        return fallback
    return [waypoint - 1 for waypoint in order if waypoint != 0]


async def _refresh_legs(db: AsyncSession, route: Route, osrm: OsrmClient | None) -> None:
    if osrm is not None:
        await recompute_legs(db, route, osrm)
        return
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
