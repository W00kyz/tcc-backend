"""Giving stops back to the pool (spec 2026-10-20-alocacao-automatica §7): only live stops are
released, a released route is paused or closed, and the silence clock reads the latest sign of
life of the route."""

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.catalog.models import FieldWorker
from app.domain.monitoring.models import WorkerPosition
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from app.domain.routing.release import last_activity_at, release_route, release_stops
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import (
    make_check_in,
    make_points,
    make_route,
    make_service_type,
    make_worker,
)

_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


async def _route(db: AsyncSession, rooms: int = 3) -> Route:
    service_type = await make_service_type(db)
    points = await make_points(
        db, service_type=service_type, names=[f"Sala {i}" for i in range(rooms)]
    )
    assert service_type.contractor_company is not None
    worker = await make_worker(db, company=service_type.contractor_company)
    route = await make_route(db, worker=worker, day=_NOW.date(), points=points)
    route.last_allocated_at = _NOW - timedelta(hours=1)
    await db.flush()
    return route


async def test_release_stops_changes_only_live_stops(db_session: AsyncSession) -> None:
    route = await _route(db_session)
    pending, in_progress, done = route.stops
    in_progress.status = RouteStopStatus.IN_PROGRESS
    done.status = RouteStopStatus.DONE
    await db_session.flush()

    released = await release_stops(
        db_session, stops=route.stops, reason=ReleaseReason.IMPEDIMENT, at=_NOW
    )

    assert {stop.id for stop in released} == {pending.id, in_progress.id}
    for stop in (pending, in_progress):
        await db_session.refresh(stop)
        assert stop.status is RouteStopStatus.RELEASED
        assert stop.released_at == _NOW
        assert stop.release_reason is ReleaseReason.IMPEDIMENT
    await db_session.refresh(done)
    assert done.status is RouteStopStatus.DONE
    assert done.released_at is None
    assert done.release_reason is None


async def test_release_stops_skips_a_stop_done_behind_the_callers_back(
    db_session: AsyncSession,
) -> None:
    # The caller's copy says PENDING, but a check-out already finished the stop: the database
    # row decides, and finished work is never handed back.
    route = await _route(db_session, rooms=1)
    stop = route.stops[0]
    await db_session.execute(
        update(RouteStop)
        .where(RouteStop.id == stop.id)
        .values(status=RouteStopStatus.DONE)
        .execution_options(synchronize_session=False)
    )

    released = await release_stops(
        db_session, stops=[stop], reason=ReleaseReason.LEFT_CAMPUS, at=_NOW
    )

    assert released == []
    await db_session.refresh(stop)
    assert stop.status is RouteStopStatus.DONE


@pytest.mark.parametrize(
    ("close", "expected"), [(False, RouteStatus.PAUSED), (True, RouteStatus.DONE)]
)
async def test_release_route_pauses_or_closes(
    db_session: AsyncSession, close: bool, expected: RouteStatus
) -> None:
    route = await _route(db_session)
    route.stops[0].status = RouteStopStatus.DONE
    await db_session.flush()

    released = await release_route(
        db_session, route=route, reason=ReleaseReason.END_OF_DAY, at=_NOW, close=close
    )

    assert {stop.id for stop in released} == {route.stops[1].id, route.stops[2].id}
    await db_session.refresh(route)
    assert route.status is expected


@pytest.mark.parametrize("latest", ["allocation", "position", "check_in"])
async def test_last_activity_at_picks_the_max(db_session: AsyncSession, latest: str) -> None:
    route = await _route(db_session, rooms=1)
    moments = {
        "allocation": _NOW - timedelta(minutes=30),
        "position": _NOW - timedelta(minutes=30),
        "check_in": _NOW - timedelta(minutes=30),
    }
    moments[latest] = _NOW - timedelta(minutes=5)
    route.last_allocated_at = moments["allocation"]
    db_session.add(
        WorkerPosition(
            field_worker_id=route.field_worker_id,
            route_id=route.id,
            latitude=-7.2150,
            longitude=-35.9100,
            reported_at=moments["position"],
        )
    )
    worker = await db_session.get(FieldWorker, route.field_worker_id)
    assert worker is not None
    await make_check_in(
        db_session, stop=route.stops[0], worker=worker, scanned_at=moments["check_in"]
    )

    assert await last_activity_at(db_session, route=route) == _NOW - timedelta(minutes=5)


async def test_last_activity_at_is_the_allocation_without_any_report(
    db_session: AsyncSession,
) -> None:
    route = await _route(db_session, rooms=1)

    assert await last_activity_at(db_session, route=route) == route.last_allocated_at
