"""The live first leg: the street path from where the worker stands now to the next stop of the
current batch. Read-only and never persisted — it changes with every step the worker takes,
while the legs between stops (`service.recompute_legs`) are written once per batch.

The next stop is the first PENDING stop of the current batch in `order_index` order — the same
order the allocation wrote and the app's "Próxima parada" (spec §4: the device never reorders).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import ServicePoint
from app.domain.routing.models import Route, RouteStop, RouteStopStatus
from app.domain.routing.osrm import OsrmClient, OsrmLeg, OsrmUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NextStopApproach:
    route_stop_id: uuid.UUID
    leg: OsrmLeg


async def next_stop_approach(
    db: AsyncSession,
    *,
    field_worker_id: uuid.UUID,
    today: date,
    origin: tuple[float, float],
    osrm: OsrmClient | None,
) -> NextStopApproach | None:
    """The street leg from `origin` (lat, lng) to the worker's next stop today, or None when
    there is no route today or no PENDING stop in its current batch. Raises `OsrmUnavailable`
    when OSRM is missing, down, or cannot route the leg — the caller must not invent a line.

        approach = await next_stop_approach(
            db, field_worker_id=worker.id, today=today, origin=(-7.215, -35.91), osrm=osrm
        )
    """
    target = await _next_pending_stop(db, field_worker_id=field_worker_id, today=today)
    if target is None:
        return None
    stop_id, destination = target
    leg = await _street_leg(osrm, origin=origin, destination=destination)
    return NextStopApproach(route_stop_id=stop_id, leg=leg)


async def _next_pending_stop(
    db: AsyncSession, *, field_worker_id: uuid.UUID, today: date
) -> tuple[uuid.UUID, tuple[float, float]] | None:
    """The id and (lat, lng) of the first PENDING stop of today's current batch."""
    route_id = await db.scalar(
        select(Route.id).where(Route.field_worker_id == field_worker_id, Route.route_date == today)
    )
    if route_id is None:
        return None
    row = (await db.execute(_first_pending_of_current_batch(route_id))).first()
    return None if row is None else (row.id, (row.latitude, row.longitude))


def _first_pending_of_current_batch(route_id: uuid.UUID) -> Select[tuple[uuid.UUID, float, float]]:
    # The current batch is the highest `batch_number` handed out — the same rule as
    # `RouteOut.current_batch`, so the server and the app agree on which stop is next.
    current_batch = (
        select(func.max(RouteStop.batch_number))
        .where(RouteStop.route_id == route_id)
        .scalar_subquery()
    )
    return (
        select(RouteStop.id, ServicePoint.latitude, ServicePoint.longitude)
        .join(ServicePoint, ServicePoint.id == RouteStop.service_point_id)
        .where(
            RouteStop.route_id == route_id,
            RouteStop.batch_number == current_batch,
            RouteStop.status == RouteStopStatus.PENDING,
        )
        .order_by(RouteStop.order_index)
        .limit(1)
    )


async def _street_leg(
    osrm: OsrmClient | None, *, origin: tuple[float, float], destination: tuple[float, float]
) -> OsrmLeg:
    """One OSRM leg origin → destination, both (lat, lng). An empty geometry is an un-routable
    leg (Ruling 6) and is reported as unavailable: a straight line is the app's call to draw."""
    if osrm is None:
        raise OsrmUnavailable("No OSRM client is configured; expected one on app.state.")
    # OSRM takes (lng, lat) — GeoJSON order.
    coordinates = [(origin[1], origin[0]), (destination[1], destination[0])]
    try:
        legs = await osrm.route_legs(coordinates)
    except OsrmUnavailable:
        logger.warning("OSRM unavailable for approach", extra={"event": "approach_osrm_down"})
        raise
    if not legs or not legs[0].geometry:
        raise OsrmUnavailable(
            f"OSRM could not route from {origin} to {destination}; expected a leg geometry."
        )
    return legs[0]
