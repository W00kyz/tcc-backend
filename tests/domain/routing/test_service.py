"""Route service layer — the legs over the FakeOsrmClient seam and closing a finished route. No
test touches the real OSRM (spec §8)."""

from datetime import date

import pytest
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.routing.models import RouteStatus, RouteStopStatus
from app.domain.routing.osrm import OsrmLeg
from app.domain.routing.service import (
    complete_route_if_finished,
    recompute_legs,
    routing_degraded,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_route, make_worker
from tests.support.osrm import FakeOsrmClient

_ROUTE_DATE = date(2026, 9, 1)


async def _seed(db: AsyncSession, *, n_points: int = 3) -> tuple[FieldWorker, list[ServicePoint]]:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([company, building])
    await db.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()

    worker = await make_worker(db, company=company, full_name="João")
    points = [
        ServicePoint(
            floor_id=floor.id,
            name=f"Ponto {i}",
            description="Sala",
            latitude=-7.20 - i * 0.01,
            longitude=-35.90 - i * 0.01,
        )
        for i in range(n_points)
    ]
    db.add_all([worker, *points])
    await db.flush()
    return worker, points


async def test_recompute_legs_writes_each_leg_from_the_previous_stop(
    db_session: AsyncSession,
) -> None:
    worker, points = await _seed(db_session, n_points=3)
    route = await make_route(db_session, worker=worker, day=_ROUTE_DATE, points=points)
    leg01 = OsrmLeg(distance_m=120.0, duration_s=90.0, geometry=[[-35.90, -7.20], [-35.91, -7.21]])
    leg12 = OsrmLeg(distance_m=200.0, duration_s=150.0, geometry=[[-35.91, -7.21], [-35.92, -7.22]])

    await recompute_legs(db_session, route, FakeOsrmClient(legs=[leg01, leg12]))

    assert route.stops[0].leg_geometry is None
    assert route.stops[1].leg_geometry == leg01.geometry
    assert route.stops[1].distance_from_prev_m == leg01.distance_m
    assert route.stops[1].duration_from_prev_s == leg01.duration_s
    assert route.stops[2].leg_geometry == leg12.geometry
    assert routing_degraded(route) is False


async def test_recompute_legs_degrades_when_osrm_is_unavailable(
    db_session: AsyncSession,
) -> None:
    worker, points = await _seed(db_session, n_points=3)
    route = await make_route(db_session, worker=worker, day=_ROUTE_DATE, points=points)
    route.stops[1].leg_geometry = [[-35.90, -7.20], [-35.91, -7.21]]  # a stale leg is cleared

    await recompute_legs(db_session, route, FakeOsrmClient(unavailable=True))

    assert all(s.leg_geometry is None for s in route.stops)
    assert all(s.distance_from_prev_m is None for s in route.stops)
    assert routing_degraded(route) is True


async def test_recompute_legs_skips_osrm_for_a_single_stop(db_session: AsyncSession) -> None:
    worker, points = await _seed(db_session, n_points=1)
    route = await make_route(db_session, worker=worker, day=_ROUTE_DATE, points=points)
    osrm = FakeOsrmClient()

    await recompute_legs(db_session, route, osrm)

    assert osrm.calls == []
    assert route.stops[0].leg_geometry is None


async def test_zeroed_leg_is_stored_as_null(db_session: AsyncSession) -> None:
    worker, points = await _seed(db_session, n_points=2)
    route = await make_route(db_session, worker=worker, day=_ROUTE_DATE, points=points)
    zeroed = OsrmLeg(distance_m=0.0, duration_s=0.0, geometry=[])

    await recompute_legs(db_session, route, FakeOsrmClient(legs=[zeroed]))

    assert route.stops[1].distance_from_prev_m is None
    assert route.stops[1].leg_geometry is None


@pytest.mark.parametrize(
    ("statuses", "closed"),
    [
        ((RouteStopStatus.DONE, RouteStopStatus.DONE), True),
        # A released stop is back in the pool, not open work of this route.
        ((RouteStopStatus.DONE, RouteStopStatus.RELEASED), True),
        ((RouteStopStatus.DONE, RouteStopStatus.PENDING), False),
        ((RouteStopStatus.DONE, RouteStopStatus.IN_PROGRESS), False),
    ],
)
async def test_complete_route_if_finished_closes_only_without_open_stops(
    db_session: AsyncSession, statuses: tuple[RouteStopStatus, ...], closed: bool
) -> None:
    worker, points = await _seed(db_session, n_points=len(statuses))
    route = await make_route(db_session, worker=worker, day=_ROUTE_DATE, points=points)
    for stop, status in zip(route.stops, statuses, strict=True):
        stop.status = status

    assert await complete_route_if_finished(db_session, route=route) is closed
    assert route.status is (RouteStatus.DONE if closed else RouteStatus.IN_PROGRESS)


async def test_complete_route_if_finished_leaves_a_paused_route_alone(
    db_session: AsyncSession,
) -> None:
    worker, points = await _seed(db_session, n_points=1)
    route = await make_route(
        db_session, worker=worker, day=_ROUTE_DATE, points=points, status=RouteStatus.PAUSED
    )
    route.stops[0].status = RouteStopStatus.RELEASED

    assert await complete_route_if_finished(db_session, route=route) is False
    assert route.status is RouteStatus.PAUSED
