"""`reassign_impediment` (Task 4, spec Ruling 1/3) — resolving an impediment by moving its
affected stop(s) onto another professional's route for the same date. The move mechanics
themselves are Task 3's `reassign_stops`; here we only prove the orchestration around it
(affected-stop resolution, target-route lookup, the same-route guard, RESOLVED bookkeeping,
and the all-or-nothing propagation). Runs against the real Postgres/PostGIS testcontainer.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.impediments import service as impediments_service
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.impediments.service import (
    AlreadyResolved,
    NoAffectedStops,
    NoRouteForDate,
    SameRouteReassignment,
    dismiss_impediment,
    reassign_impediment,
)
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    RouteType,
    StopAssignment,
)
from app.domain.routing.service import StopInput, StopNotPending, create_route
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.osrm import FakeOsrmClient

_ROUTE_DATE = date(2026, 9, 1)
_PASSWORD = "senha-forte-o-suficiente"


async def _seed(
    db: AsyncSession, *, n_points: int
) -> tuple[User, FieldWorker, FieldWorker, list[ServicePoint]]:
    manager_user = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_a_user = User(
        name="Joao",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    worker_b_user = User(
        name="Maria",
        email="maria@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([manager_user, worker_a_user, worker_b_user, company, building])
    await db.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()

    worker_a = FieldWorker(
        full_name="João", contractor_company_id=company.id, user_id=worker_a_user.id
    )
    worker_b = FieldWorker(
        full_name="Maria", contractor_company_id=company.id, user_id=worker_b_user.id
    )
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
    return manager_user, worker_a, worker_b, points


async def _make_impediment(
    db: AsyncSession,
    *,
    route_id: uuid.UUID,
    reported_by: uuid.UUID,
    scope: ImpedimentScope,
    route_stop_id: uuid.UUID | None = None,
) -> Impediment:
    imp = Impediment(
        route_id=route_id,
        route_stop_id=route_stop_id,
        scope=scope,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by=reported_by,
        reported_at=datetime.now(UTC),
    )
    db.add(imp)
    await db.flush()
    return imp


async def _assignments(db: AsyncSession, stop_id: uuid.UUID) -> list[StopAssignment]:
    rows = await db.scalars(
        select(StopAssignment)
        .where(StopAssignment.route_stop_id == stop_id)
        .order_by(StopAssignment.sequence)
    )
    return list(rows)


async def _build_routes(
    db: AsyncSession, *, source_points: int = 2, target_points: int = 1
) -> tuple[User, FieldWorker, FieldWorker, Route, Route]:
    manager, worker_a, worker_b, points = await _seed(db, n_points=source_points + target_points)
    source = await create_route(
        db,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:source_points]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    source.status = RouteStatus.IN_PROGRESS
    target = await create_route(
        db,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[source_points:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    await db.flush()
    return manager, worker_a, worker_b, source, target


async def test_reassign_impediment_stop_scope_moves_stop_and_resolves(
    db_session: AsyncSession,
) -> None:
    manager, worker_a, worker_b, source, target = await _build_routes(db_session)
    moved_stop_id = source.stops[0].id
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.STOP,
        route_stop_id=moved_stop_id,
    )

    resolved = await reassign_impediment(
        db_session,
        impediment=imp,
        new_field_worker_id=worker_b.id,
        reason="afastamento médico",
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    moved = await db_session.get(RouteStop, moved_stop_id)
    assert moved is not None
    assert moved.route_id == target.id
    rows = await _assignments(db_session, moved_stop_id)
    assert [a.sequence for a in rows] == [1, 2]
    assert rows[1].field_worker_id == worker_b.id
    assert resolved.status is ImpedimentStatus.RESOLVED
    assert resolved.resolved_by == manager.id
    assert resolved.resolved_at is not None


async def test_reassign_impediment_route_remainder_moves_every_pending_stop(
    db_session: AsyncSession,
) -> None:
    manager, worker_a, worker_b, source, target = await _build_routes(
        db_session, source_points=3, target_points=1
    )
    source.stops[0].status = RouteStopStatus.DONE  # only stops 2 and 3 are still pending
    await db_session.flush()
    pending_ids = [source.stops[1].id, source.stops[2].id]
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.ROUTE_REMAINDER,
    )

    await reassign_impediment(
        db_session,
        impediment=imp,
        new_field_worker_id=worker_b.id,
        reason="veículo quebrou",
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )

    for stop_id in pending_ids:
        stop = await db_session.get(RouteStop, stop_id)
        assert stop is not None
        assert stop.route_id == target.id
    assert imp.status is ImpedimentStatus.RESOLVED


async def test_reassign_impediment_rejects_already_resolved(db_session: AsyncSession) -> None:
    manager, worker_a, worker_b, source, _target = await _build_routes(db_session)
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.STOP,
        route_stop_id=source.stops[0].id,
    )
    await dismiss_impediment(db_session, impediment=imp, actor_id=manager.id)

    with pytest.raises(AlreadyResolved):
        await reassign_impediment(
            db_session,
            impediment=imp,
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_impediment_rejects_worker_without_route_on_date(
    db_session: AsyncSession,
) -> None:
    manager, worker_a, _worker_b, source, _target = await _build_routes(db_session)
    # A brand-new field worker with no route at all.
    orphan = FieldWorker(full_name="Sem Rota", contractor_company_id=worker_a.contractor_company_id)
    db_session.add(orphan)
    await db_session.flush()
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.STOP,
        route_stop_id=source.stops[0].id,
    )

    with pytest.raises(NoRouteForDate):
        await reassign_impediment(
            db_session,
            impediment=imp,
            new_field_worker_id=orphan.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_impediment_rejects_route_remainder_with_no_pending(
    db_session: AsyncSession,
) -> None:
    manager, worker_a, worker_b, source, _target = await _build_routes(
        db_session, source_points=1, target_points=1
    )
    source.stops[0].status = RouteStopStatus.DONE
    await db_session.flush()
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.ROUTE_REMAINDER,
    )

    with pytest.raises(NoAffectedStops):
        await reassign_impediment(
            db_session,
            impediment=imp,
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )


async def test_reassign_impediment_rejects_same_route_target(db_session: AsyncSession) -> None:
    manager, worker_a, _worker_b, source, _target = await _build_routes(db_session)
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.STOP,
        route_stop_id=source.stops[0].id,
    )

    # worker_a is the impeded route's own worker → resolving the target route lands on `source`.
    with pytest.raises(SameRouteReassignment):
        await reassign_impediment(
            db_session,
            impediment=imp,
            new_field_worker_id=worker_a.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )
    assert imp.status is ImpedimentStatus.PENDING


async def test_reassign_impediment_mixed_batch_is_all_or_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU guard: if one stop in a ROUTE_REMAINDER batch became non-PENDING between
    impediment creation and reassignment, `reassign_stops` rejects the whole batch and no
    stop is moved. Simulated by having `_affected_stops` hand back a stale (now-DONE) stop
    alongside the good ones."""
    manager, worker_a, worker_b, source, _target = await _build_routes(
        db_session, source_points=3, target_points=1
    )
    good_ids = [source.stops[0].id, source.stops[1].id]
    stale_stop = source.stops[2]
    stale_stop.status = RouteStopStatus.DONE
    await db_session.flush()
    imp = await _make_impediment(
        db_session,
        route_id=source.id,
        reported_by=worker_a.id,
        scope=ImpedimentScope.ROUTE_REMAINDER,
    )

    real_stops = [source.stops[0], source.stops[1], stale_stop]

    async def _stale_affected(db: AsyncSession, impediment: Impediment) -> list[RouteStop]:
        return real_stops

    monkeypatch.setattr(impediments_service, "_affected_stops", _stale_affected)

    with pytest.raises(StopNotPending):
        await reassign_impediment(
            db_session,
            impediment=imp,
            new_field_worker_id=worker_b.id,
            reason="reason",
            actor_id=manager.id,
            osrm=FakeOsrmClient(),
        )

    # Nothing moved: both good stops still on the source route with a single assignment.
    for stop_id in good_ids:
        stop = await db_session.get(RouteStop, stop_id)
        assert stop is not None
        assert stop.route_id == source.id
        assert len(await _assignments(db_session, stop_id)) == 1
    assert imp.status is ImpedimentStatus.PENDING
