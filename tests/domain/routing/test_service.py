"""Route service layer (Task 3) — create / replace stops / optimize / reassign / cancel /
legs, all over the FakeOsrmClient seam. No test touches the real OSRM (spec §8)."""

import uuid
from datetime import date

import pytest
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import (
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    RouteType,
    StopAssignment,
    StopAssignmentOutcome,
)
from app.domain.routing.osrm import OsrmLeg, OsrmUnavailable
from app.domain.routing.service import (
    DoneStopRemoved,
    RouteNotEditable,
    StopInput,
    UnknownServicePoint,
    cancel_route,
    create_route,
    optimize_route,
    reassign_route,
    replace_route_stops,
    routing_degraded,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.osrm import FakeOsrmClient

_ROUTE_DATE = date(2026, 9, 1)


async def _seed(
    db: AsyncSession, *, n_points: int = 3
) -> tuple[User, FieldWorker, FieldWorker, list[ServicePoint]]:
    manager = User(
        name="Larissa", email="larissa@pu.ufcg.edu.br", password_hash="x", role=UserRole.MANAGER
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([manager, company, building])
    await db.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()

    worker_a = FieldWorker(full_name="João", contractor_company_id=company.id)
    worker_b = FieldWorker(full_name="Maria", contractor_company_id=company.id)
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
    db.add_all([worker_a, worker_b, *points])
    await db.flush()
    return manager, worker_a, worker_b, points


async def _assignments(db: AsyncSession, stop_id: uuid.UUID) -> list[StopAssignment]:
    result = await db.scalars(
        select(StopAssignment)
        .where(StopAssignment.route_stop_id == stop_id)
        .order_by(StopAssignment.sequence)
    )
    return list(result)


async def test_create_route_orders_stops_and_writes_legs(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)
    leg01 = OsrmLeg(distance_m=120.0, duration_s=90.0, geometry=[[-35.90, -7.20], [-35.91, -7.21]])
    leg12 = OsrmLeg(distance_m=200.0, duration_s=150.0, geometry=[[-35.91, -7.21], [-35.92, -7.22]])
    osrm = FakeOsrmClient(legs=[leg01, leg12])

    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=osrm,
    )

    assert route.status is RouteStatus.PLANNED
    assert [s.order_index for s in route.stops] == [1, 2, 3]
    assert [s.service_point_id for s in route.stops] == [p.id for p in points]
    assert route.stops[0].leg_geometry is None
    assert route.stops[1].leg_geometry == leg01.geometry
    assert route.stops[1].distance_from_prev_m == leg01.distance_m
    assert route.stops[1].duration_from_prev_s == leg01.duration_s
    assert route.stops[2].leg_geometry == leg12.geometry
    assert routing_degraded(route) is False

    for stop in route.stops:
        rows = await _assignments(db_session, stop.id)
        assert [a.sequence for a in rows] == [1]
        assert rows[0].assigned_by == manager.id
        assert rows[0].field_worker_id == worker_a.id


async def test_create_route_degraded_when_osrm_unavailable(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)

    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(unavailable=True),
    )

    assert all(s.leg_geometry is None for s in route.stops)
    assert all(s.distance_from_prev_m is None for s in route.stops)
    assert routing_degraded(route) is True


async def test_replace_stops_reorders_and_recomputes(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)
    point_a, point_b, point_c = points
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=point_a.id), StopInput(service_point_id=point_b.id)],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    # A different manager runs the PATCH — the new stop's assignment must be attributed to them,
    # not to the route's original creator (spec §3.4, Ruling 1).
    editor = User(
        name="Rafael", email="rafael@pu.ufcg.edu.br", password_hash="x", role=UserRole.MANAGER
    )
    db_session.add(editor)
    await db_session.flush()

    route = await replace_route_stops(
        db_session,
        route=route,
        stops=[
            StopInput(service_point_id=point_b.id),
            StopInput(service_point_id=point_a.id),
            StopInput(service_point_id=point_c.id),
        ],
        actor_id=editor.id,
        osrm=FakeOsrmClient(),
    )

    assert [s.service_point_id for s in route.stops] == [point_b.id, point_a.id, point_c.id]
    assert [s.order_index for s in route.stops] == [1, 2, 3]
    assert route.stops[0].leg_geometry is None
    assert route.stops[1].leg_geometry is not None
    new_stop = next(s for s in route.stops if s.service_point_id == point_c.id)
    rows = await _assignments(db_session, new_stop.id)
    assert [a.sequence for a in rows] == [1]
    assert rows[0].assigned_by == editor.id


async def test_replace_stops_drops_pending_stop(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)
    point_a, point_b, point_c = points
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    dropped_stop_id = next(s.id for s in route.stops if s.service_point_id == point_b.id)

    route = await replace_route_stops(
        db_session,
        route=route,
        stops=[
            StopInput(service_point_id=point_a.id),
            StopInput(service_point_id=point_c.id),
        ],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    assert [s.service_point_id for s in route.stops] == [point_a.id, point_c.id]
    assert [s.order_index for s in route.stops] == [1, 2]
    assert await db_session.get(RouteStop, dropped_stop_id) is None
    assert await _assignments(db_session, dropped_stop_id) == []
    assert route.stops[0].leg_geometry is None
    assert route.stops[1].leg_geometry is not None


async def test_replace_stops_rejects_removing_done_stop(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=2)
    point_a, point_b = points
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=point_a.id), StopInput(service_point_id=point_b.id)],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    route.stops[0].status = RouteStopStatus.DONE
    await db_session.flush()

    with pytest.raises(DoneStopRemoved):
        await replace_route_stops(
            db_session,
            route=route,
            stops=[StopInput(service_point_id=point_b.id)],
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_replace_stops_rejects_cancelled_route(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=2)
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    route.status = RouteStatus.CANCELLED
    await db_session.flush()

    with pytest.raises(RouteNotEditable):
        await replace_route_stops(
            db_session,
            route=route,
            stops=[StopInput(service_point_id=points[0].id)],
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_optimize_route_applies_permutation(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    route = await optimize_route(db_session, route=route, osrm=FakeOsrmClient(order=[0, 2, 1]))

    assert [s.service_point_id for s in route.stops] == [
        points[0].id,
        points[2].id,
        points[1].id,
    ]
    assert [s.order_index for s in route.stops] == [1, 2, 3]


async def test_optimize_route_raises_when_unavailable(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=3)
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    with pytest.raises(OsrmUnavailable):
        await optimize_route(db_session, route=route, osrm=FakeOsrmClient(unavailable=True))


async def test_reassign_route_appends_stop_assignments(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=3)
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    done_stop_id = route.stops[2].id
    route.stops[2].status = RouteStopStatus.DONE
    await db_session.flush()

    route = await reassign_route(
        db_session,
        route=route,
        new_field_worker_id=worker_b.id,
        reason="ausência",
        actor_id=manager.id,
    )

    assert route.field_worker_id == worker_b.id
    for stop in route.stops[:2]:
        rows = await _assignments(db_session, stop.id)
        assert [a.sequence for a in rows] == [1, 2]
        assert rows[0].field_worker_id == worker_a.id
        assert rows[0].outcome is StopAssignmentOutcome.REASSIGNED
        assert rows[1].field_worker_id == worker_b.id
        assert rows[1].transfer_reason == "ausência"
        assert rows[1].assigned_by == manager.id

    done_rows = await _assignments(db_session, done_stop_id)
    assert [a.sequence for a in done_rows] == [1]
    assert done_rows[0].outcome is None


async def test_cancel_route_sets_status_and_reason(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, points = await _seed(db_session, n_points=2)
    route = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    route = await cancel_route(db_session, route=route, reason="evento cancelado pela PU")

    assert route.status is RouteStatus.CANCELLED
    assert route.cancellation_reason == "evento cancelado pela PU"
    for stop in route.stops:
        rows = await _assignments(db_session, stop.id)
        assert rows[0].outcome is StopAssignmentOutcome.CANCELLED

    with pytest.raises(RouteNotEditable):
        await cancel_route(db_session, route=route, reason="de novo")


async def test_unknown_service_point_rejected(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, _points = await _seed(db_session, n_points=1)

    with pytest.raises(UnknownServicePoint):
        await create_route(
            db_session,
            field_worker_id=worker_a.id,
            route_date=_ROUTE_DATE,
            route_type=RouteType.REGULAR,
            scheduled_start_at=None,
            stops=[StopInput(service_point_id=uuid.uuid4())],
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )
