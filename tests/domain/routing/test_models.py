"""Route/stop model defaults and the two exclusivity guards of spec 2026-10-20-alocacao-automatica
§5.3: one route per worker per day, and a point live on at most one stop per day."""

from datetime import date

import pytest
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.routing.models import ReleaseReason, Route, RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_route, make_worker

_DAY = date(2026, 9, 1)


async def _seed(db: AsyncSession) -> tuple[list[FieldWorker], ServicePoint]:
    company = ContractorCompany(name="C", cnpj="12345678000199")
    building = Building(name="B", campus_area="A")
    db.add_all([company, building])
    await db.flush()
    floor = Floor(building_id=building.id, label="T")
    workers = [
        await make_worker(db, company=company, full_name="W1"),
        await make_worker(db, company=company, full_name="W2"),
    ]
    db.add_all([floor, *workers])
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name="P", description="d", latitude=-7.2, longitude=-35.9
    )
    db.add(point)
    await db.flush()
    return workers, point


async def test_route_starts_in_progress(db_session: AsyncSession) -> None:
    workers, _point = await _seed(db_session)
    route = Route(field_worker_id=workers[0].id, route_date=_DAY)
    db_session.add(route)
    await db_session.commit()

    loaded = await db_session.scalar(select(Route).where(Route.id == route.id))
    assert loaded is not None
    assert loaded.status is RouteStatus.IN_PROGRESS
    assert loaded.last_allocated_at is None


async def test_route_stop_leg_and_release_columns_round_trip(db_session: AsyncSession) -> None:
    workers, point = await _seed(db_session)
    route = await make_route(db_session, worker=workers[0], day=_DAY, points=[point])
    stop = route.stops[0]
    stop.distance_from_prev_m = 12.5
    stop.leg_geometry = [[-35.9, -7.2], [-35.89, -7.19]]
    stop.status = RouteStopStatus.RELEASED
    stop.release_reason = ReleaseReason.LOCATION_SILENCE
    await db_session.commit()

    loaded = await db_session.scalar(select(RouteStop).where(RouteStop.id == stop.id))
    assert loaded is not None
    assert loaded.leg_geometry == [[-35.9, -7.2], [-35.89, -7.19]]
    assert loaded.distance_from_prev_m == 12.5
    assert (loaded.batch_number, loaded.service_date) == (1, _DAY)
    assert loaded.release_reason is ReleaseReason.LOCATION_SILENCE


async def test_one_route_per_worker_per_day(db_session: AsyncSession) -> None:
    workers, _point = await _seed(db_session)
    db_session.add(Route(field_worker_id=workers[0].id, route_date=_DAY))
    await db_session.flush()

    db_session.add(Route(field_worker_id=workers[0].id, route_date=_DAY))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_point_is_live_on_one_stop_per_day(db_session: AsyncSession) -> None:
    workers, point = await _seed(db_session)
    await make_route(db_session, worker=workers[0], day=_DAY, points=[point])

    with pytest.raises(IntegrityError):
        await make_route(db_session, worker=workers[1], day=_DAY, points=[point])


async def test_a_released_point_can_be_claimed_again(db_session: AsyncSession) -> None:
    workers, point = await _seed(db_session)
    first = await make_route(db_session, worker=workers[0], day=_DAY, points=[point])
    first.stops[0].status = RouteStopStatus.RELEASED
    await db_session.flush()

    second = await make_route(db_session, worker=workers[1], day=_DAY, points=[point])
    other_day = await make_route(
        db_session, worker=workers[0], day=date(2026, 9, 2), points=[point]
    )

    assert second.stops[0].service_point_id == point.id
    assert other_day.stops[0].service_point_id == point.id
