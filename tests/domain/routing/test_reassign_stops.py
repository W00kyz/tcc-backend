"""`reassign_stops` (Task 3, spec §3.4 Ruling 1) — moving a subset of PENDING stops from one
route to another, reusing the append-only stop_assignments chain. No test touches the real
OSRM (spec §8)."""

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
    RouteStopStatus,
    RouteType,
    StopAssignment,
    StopAssignmentOutcome,
)
from app.domain.routing.service import (
    RouteNotEditable,
    StopInput,
    StopNotOnRoute,
    StopNotReassignable,
    UnknownFieldWorker,
    create_route,
    reassign_stops,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.osrm import FakeOsrmClient

_ROUTE_DATE = date(2026, 9, 1)


async def _seed(
    db: AsyncSession, *, n_points: int = 4
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


async def test_reassign_stops_moves_stop_and_reindexes_both_routes(
    db_session: AsyncSession,
) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    moved_stop = source.stops[0]
    moved_stop_id = moved_stop.id
    remaining_stop_id = source.stops[1].id
    first_assignment = (await _assignments(db_session, moved_stop_id))[0]

    source_osrm = FakeOsrmClient()

    source, target = await reassign_stops(
        db_session,
        source_route=source,
        target_route=target,
        stop_ids=[moved_stop_id],
        new_field_worker_id=worker_b.id,
        reason="afastamento médico",
        actor_id=manager.id,
        osrm=source_osrm,
    )

    moved = next(s for s in target.stops if s.id == moved_stop_id)
    assert moved.route_id == target.id
    assert moved.order_index == 3  # goes to the end of target (which had 2 stops)
    assert moved.status is RouteStopStatus.PENDING

    assert [s.id for s in source.stops] == [remaining_stop_id]
    assert [s.order_index for s in source.stops] == [1]  # reindexed without gaps

    rows = await _assignments(db_session, moved_stop_id)
    assert [a.sequence for a in rows] == [1, 2]
    assert rows[0].id == first_assignment.id
    assert rows[0].outcome is StopAssignmentOutcome.REASSIGNED
    assert rows[1].field_worker_id == worker_b.id
    assert rows[1].assigned_by == manager.id
    assert rows[1].transfer_reason == "afastamento médico"
    assert rows[1].outcome is None


async def test_reassign_stops_recomputes_legs_on_both_routes(db_session: AsyncSession) -> None:
    # Source keeps 3 points (2 remain after the move) so its leg recomputation still fires
    # route_legs with >= 2 stops — proving _recompute_legs ran on BOTH routes, not just target.
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=5)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:3]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[3:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    moved_stop_id = source.stops[0].id

    osrm = FakeOsrmClient()
    source, target = await reassign_stops(
        db_session,
        source_route=source,
        target_route=target,
        stop_ids=[moved_stop_id],
        new_field_worker_id=worker_b.id,
        reason="reason",
        actor_id=manager.id,
        osrm=osrm,
    )

    # _recompute_legs ran on both: at least two route_legs calls, one per route.
    route_legs_calls = [c for c in osrm.calls if c[0] == "route_legs"]
    assert len(route_legs_calls) == 2
    # target now has 3 stops so its leg computation used all 3 points.
    assert any(len(coords) == 3 for _, coords in route_legs_calls)


async def test_reassign_stops_all_pending_stops_route_remainder(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    stop_ids = [s.id for s in source.stops]

    source, target = await reassign_stops(
        db_session,
        source_route=source,
        target_route=target,
        stop_ids=stop_ids,
        new_field_worker_id=worker_b.id,
        reason="rota inteira remanejada",
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    assert source.stops == []
    assert set(stop_ids).issubset({s.id for s in target.stops})
    assert len(target.stops) == 4
    for stop_id in stop_ids:
        rows = await _assignments(db_session, stop_id)
        assert [a.sequence for a in rows] == [1, 2]
        assert rows[1].field_worker_id == worker_b.id


async def test_reassign_stops_rejects_stop_not_on_source_route(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    foreign_stop_id = target.stops[0].id

    with pytest.raises(StopNotOnRoute):
        await reassign_stops(
            db_session,
            source_route=source,
            target_route=target,
            stop_ids=[foreign_stop_id],
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_stops_rejects_done_stop(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    done_stop_id = source.stops[0].id
    source.stops[0].status = RouteStopStatus.DONE
    await db_session.flush()

    with pytest.raises(StopNotReassignable):
        await reassign_stops(
            db_session,
            source_route=source,
            target_route=target,
            stop_ids=[done_stop_id],
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_stops_moves_in_progress_stop_and_resets_it_to_pending(
    db_session: AsyncSession,
) -> None:
    # F1 fix (Ruling P3): an IN_PROGRESS stop (impeded worker checked in, then stuck) still
    # moves — and the substitute starts it clean, so its status is reset to PENDING.
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    moved_stop_id = source.stops[0].id
    source.stops[0].status = RouteStopStatus.IN_PROGRESS
    await db_session.flush()

    await reassign_stops(
        db_session,
        source_route=source,
        target_route=target,
        stop_ids=[moved_stop_id],
        new_field_worker_id=worker_b.id,
        reason="reason",
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    moved = next(s for s in target.stops if s.id == moved_stop_id)
    assert moved.route_id == target.id
    assert moved.status == RouteStopStatus.PENDING


async def test_reassign_stops_rejects_not_editable_source_route(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    source.status = RouteStatus.CANCELLED
    await db_session.flush()

    with pytest.raises(RouteNotEditable):
        await reassign_stops(
            db_session,
            source_route=source,
            target_route=target,
            stop_ids=[source.stops[0].id],
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_stops_rejects_not_editable_target_route(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target.status = RouteStatus.DONE
    await db_session.flush()

    with pytest.raises(RouteNotEditable):
        await reassign_stops(
            db_session,
            source_route=source,
            target_route=target,
            stop_ids=[source.stops[0].id],
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_stops_rejects_unknown_field_worker(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, points = await _seed(db_session, n_points=4)
    source = await create_route(
        db_session,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:2]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    target = await create_route(
        db_session,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[2:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    with pytest.raises(UnknownFieldWorker):
        await reassign_stops(
            db_session,
            source_route=source,
            target_route=target,
            stop_ids=[source.stops[0].id],
            new_field_worker_id=uuid.uuid4(),
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )
