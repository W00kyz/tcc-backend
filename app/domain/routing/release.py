"""Giving a worker's points back to the shared daily pool (spec 2026-10-20-alocacao-automatica
§7). Every release turns the live stops — PENDING or IN_PROGRESS, no check-out received — into
RELEASED with the reason; `free_points` then offers the point to whoever asks next.

Transaction discipline: these functions flush, never commit — the impediment endpoint and the
release scan own the transaction.

    released = await release_route(db, route=route, reason=ReleaseReason.LEFT_CAMPUS, at=now)
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution.models import Execution, QrScan
from app.domain.monitoring.models import WorkerPosition
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)

LIVE_STOP_STATUSES = (RouteStopStatus.PENDING, RouteStopStatus.IN_PROGRESS)


async def release_stops(
    db: AsyncSession, *, stops: Sequence[RouteStop], reason: ReleaseReason, at: datetime
) -> list[RouteStop]:
    """Release the live stops among `stops`; returns the ones that changed.

    IMPORTANT: the status guard runs in the UPDATE itself, not on the caller's copy. A check-out
    committed after the caller loaded `stops` left the row DONE, and finished work must never go
    back to the pool — the row decides, a stale in-memory PENDING does not.

        released = await release_stops(db, stops=[stop], reason=ReleaseReason.IMPEDIMENT, at=now)
    """
    candidate_ids = [stop.id for stop in stops if stop.status in LIVE_STOP_STATUSES]
    if not candidate_ids:
        return []
    result = await db.execute(
        update(RouteStop)
        .where(RouteStop.id.in_(candidate_ids), RouteStop.status.in_(LIVE_STOP_STATUSES))
        .values(status=RouteStopStatus.RELEASED, released_at=at, release_reason=reason)
        .returning(RouteStop.id)
        # "fetch": only the rows the UPDATE matched are refreshed in the session; "evaluate"
        # would re-run the guard on the stale in-memory status and mark the DONE row too.
        .execution_options(synchronize_session="fetch")
    )
    released_ids = set(result.scalars())
    return [stop for stop in stops if stop.id in released_ids]


async def release_route(
    db: AsyncSession, *, route: Route, reason: ReleaseReason, at: datetime, close: bool = False
) -> list[RouteStop]:
    """Release every live stop of `route`, then pause it — "Iniciar rota" reopens it with a new
    batch — or, with `close`, end it for the day (`DONE`). Returns the released stops.

        await release_route(db, route=route, reason=ReleaseReason.END_OF_DAY, at=now, close=True)
    """
    live = await db.scalars(
        select(RouteStop).where(
            RouteStop.route_id == route.id, RouteStop.status.in_(LIVE_STOP_STATUSES)
        )
    )
    released = await release_stops(db, stops=list(live), reason=reason, at=at)
    route.status = RouteStatus.DONE if close else RouteStatus.PAUSED
    await db.flush()
    return released


async def last_activity_at(db: AsyncSession, *, route: Route) -> datetime:
    """The latest sign of life of the route's worker: the last batch handed out, the last GPS
    position, or the last QR scan (check-in or check-out) — the location-silence clock (spec
    §7.3). A scan counts so that a worker without background-location permission, working with
    the app open, is never taken for silent. Device clocks (`reported_at`, `scanned_at`), the
    same ones the app's own silence warning reads.

        silent = await last_activity_at(db, route=route) < now - timedelta(minutes=45)
    """
    last_position = await db.scalar(
        select(func.max(WorkerPosition.reported_at)).where(WorkerPosition.route_id == route.id)
    )
    last_scan = await db.scalar(
        select(func.max(QrScan.scanned_at))
        .join(Execution, QrScan.execution_id == Execution.id)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .where(RouteStop.route_id == route.id)
    )
    # A route with stops always has `last_allocated_at`; `created_at` only backs a route that
    # never got a batch, so the clock still starts somewhere.
    moments = [route.last_allocated_at, last_position, last_scan]
    return max((moment for moment in moments if moment is not None), default=route.created_at)
