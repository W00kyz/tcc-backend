"""The on-demand release scan: `--now` parsing, and one pass that releases a silent worker's batch
exactly as the scheduler's minute would."""

from datetime import UTC, datetime, timedelta

from app.core.campus_clock import CAMPUS_TIMEZONE, campus_today
from app.core.config import Settings
from app.domain.routing.models import ReleaseReason, RouteStatus, RouteStop, RouteStopStatus
from app.scripts.run_release_scan import parse_now, release_once
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

# Tuesday 10:00 on campus (UTC-3): a service day, before the end of the workday.
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def test_parse_now_defaults_to_the_real_clock() -> None:
    before = datetime.now(UTC)

    assert before <= parse_now(None) <= datetime.now(UTC)


def test_parse_now_keeps_an_explicit_offset() -> None:
    assert parse_now("2026-10-20T13:00:00+00:00") == _NOW


def test_parse_now_reads_a_bare_datetime_as_campus_time() -> None:
    parsed = parse_now("2026-10-20T10:00")

    assert parsed == _NOW
    assert parsed.tzinfo == CAMPUS_TIMEZONE


async def test_release_once_releases_a_silent_batch(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    service_type = await make_service_type(db_session)
    points = await make_points(db_session, service_type=service_type, names=["Sala 1", "Sala 2"])
    assert service_type.contractor_company is not None
    worker = await make_worker(db_session, company=service_type.contractor_company)
    route = await make_route(db_session, worker=worker, day=campus_today(_NOW), points=points)
    # Nothing heard from the worker since the batch, 46 min ago: past the 45-min default.
    route.started_at = route.last_allocated_at = _NOW - timedelta(minutes=46)
    await db_session.commit()

    raised = await release_once(test_settings.database_url, _NOW)

    assert raised == 1  # the BATCH_RELEASED alert
    await db_session.refresh(route)
    assert route.status is RouteStatus.PAUSED
    stops = (
        await db_session.scalars(select(RouteStop).where(RouteStop.route_id == route.id))
    ).all()
    assert {(stop.status, stop.release_reason) for stop in stops} == {
        (RouteStopStatus.RELEASED, ReleaseReason.LOCATION_SILENCE)
    }
