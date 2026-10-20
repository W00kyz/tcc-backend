"""Route service layer over the OSRM seam. Every function `flush`es but never `commit`s — the
caller owns the transaction, mirroring `app/domain/execution/service.py`'s callers.

Routes are no longer built by hand (spec 2026-10-20-alocacao-automatica §2.1): the automatic
allocation creates them and calls `recompute_legs` after each batch. An un-routable leg or an
OSRM outage degrades to `NULL` legs, it never blocks the write (Ruling 6).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import ServicePoint
from app.domain.routing.models import Route, RouteStop, RouteStopStatus
from app.domain.routing.osrm import OsrmClient, OsrmLeg, OsrmUnavailable


def routing_degraded(route: Route) -> bool:
    """True when any PENDING stop after the first has no leg geometry — the single, uniform
    "OSRM could not draw this leg" signal (spec §4.2, Ruling 6). `route.stops` must be loaded;
    `recompute_legs` leaves it loaded and ordered."""
    ordered = sorted(route.stops, key=lambda s: s.order_index)
    return any(
        stop.leg_geometry is None for stop in ordered[1:] if stop.status == RouteStopStatus.PENDING
    )


async def recompute_legs(db: AsyncSession, route: Route, osrm: OsrmClient) -> None:
    """Rewrite `distance_from_prev_m` / `duration_from_prev_s` / `leg_geometry` on every stop
    in the current order. `stops[0]` always gets `None` (no previous stop). A zeroed leg or an
    OSRM outage stores `None` in all three columns, so `leg_geometry is None` is the uniform
    degraded signal (`routing_degraded`). Flushes and leaves `route.stops` loaded and ordered.

        await recompute_legs(db, route, request.app.state.osrm_client)
    """
    await db.flush()
    stops = await _ordered_stops(db, route)
    legs = await _fetch_legs(db, stops, osrm)
    for stop in stops:
        _clear_leg(stop)
    for stop, leg in zip(stops[1:], legs, strict=False):
        if not leg.geometry:
            continue  # zeroed leg — leave the three columns None (Ruling 6).
        stop.distance_from_prev_m = leg.distance_m
        stop.duration_from_prev_s = leg.duration_s
        stop.leg_geometry = leg.geometry

    await db.flush()
    await db.refresh(route, attribute_names=["stops"])


async def _fetch_legs(db: AsyncSession, stops: list[RouteStop], osrm: OsrmClient) -> list[OsrmLeg]:
    """The OSRM legs between consecutive stops — none with fewer than two stops, and none when
    OSRM is down (degraded mode, Ruling 6: every leg stays NULL)."""
    if len(stops) < 2:
        return []
    points = await _points_by_id(db, [s.service_point_id for s in stops])
    coordinates = [
        (points[s.service_point_id].longitude, points[s.service_point_id].latitude) for s in stops
    ]
    try:
        return await osrm.route_legs(coordinates)
    except OsrmUnavailable:
        return []


def _clear_leg(stop: RouteStop) -> None:
    stop.distance_from_prev_m = None
    stop.duration_from_prev_s = None
    stop.leg_geometry = None


async def _ordered_stops(db: AsyncSession, route: Route) -> list[RouteStop]:
    result = await db.scalars(
        select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.order_index)
    )
    return list(result)


async def _points_by_id(
    db: AsyncSession, service_point_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ServicePoint]:
    result = await db.scalars(
        select(ServicePoint).where(ServicePoint.id.in_(set(service_point_ids)))
    )
    return {point.id: point for point in result}
