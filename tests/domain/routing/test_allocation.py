"""The automatic allocation (spec 2026-10-20-alocacao-automatica §6): the pool of a worker's
company, batches of the nearest free points, exclusivity between workers, and the refusals that
must leave no route behind. OSRM is always the FakeOsrmClient (spec §8)."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.campus_clock import campus_today
from app.core.config import Settings
from app.db.session import build_engine, build_session_factory
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.impediments.models import Impediment, ImpedimentReason, ImpedimentScope
from app.domain.routing import allocation
from app.domain.routing.allocation import (
    AllocationResult,
    NotAServiceDay,
    OutsideCampus,
    WorkdayOver,
    next_batch,
    start_or_resume,
)
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support.factories import make_worker
from tests.support.osrm import FakeOsrmClient

# Tuesday 10:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
_TODAY = campus_today(_NOW)
_SATURDAY_NOW = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)
_FIVE_PM = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
# Where the worker taps "Iniciar rota", inside the campus rectangle.
_ORIGIN = (-7.2150, -35.9100)


@dataclass
class _Company:
    company: ContractorCompany
    service_type: ServiceType


async def _company(db: AsyncSession, name: str = "Limpa Tudo") -> _Company:
    company = ContractorCompany(name=name, cnpj=str(uuid4().int)[:14])
    db.add(company)
    await db.flush()
    service_type = ServiceType(
        name=f"Limpeza {name}", average_duration_minutes=30, contractor_company_id=company.id
    )
    db.add(service_type)
    await db.flush()
    return _Company(company=company, service_type=service_type)


async def _building(
    db: AsyncSession,
    name: str,
    at: tuple[float, float],
    rooms: Sequence[tuple[str, str]],
    service_type: ServiceType | None,
) -> list[ServicePoint]:
    """A building at `at` (lat, lng) with one point per (floor label, room name). Every point
    sits on the building's centre, as Part A derives them."""
    building = Building(name=name, campus_area="CCT")
    db.add(building)
    await db.flush()
    floors: dict[str, Floor] = {}
    for label in dict.fromkeys(label for label, _ in rooms):
        floors[label] = Floor(building_id=building.id, label=label)
        db.add(floors[label])
    await db.flush()
    points = [
        ServicePoint(
            floor_id=floors[label].id,
            name=room,
            description="Sala",
            latitude=at[0],
            longitude=at[1],
            service_type_id=service_type.id if service_type is not None else None,
        )
        for label, room in rooms
    ]
    db.add_all(points)
    await db.flush()
    return points


async def _settings(db: AsyncSession, *, batch_size: int = 4) -> SystemSettings:
    settings = await get_settings_row(db)
    settings.allocation_batch_size = batch_size
    await db.flush()
    return settings


async def _start(
    db: AsyncSession,
    worker: FieldWorker,
    settings: SystemSettings,
    *,
    now: datetime = _NOW,
    at: tuple[float, float] = _ORIGIN,
    osrm: FakeOsrmClient | None = None,
) -> AllocationResult:
    return await start_or_resume(
        db,
        worker=worker,
        today=campus_today(now),
        now=now,
        lat=at[0],
        lng=at[1],
        settings=settings,
        osrm=osrm or FakeOsrmClient(),
    )


async def _next(
    db: AsyncSession,
    route: Route,
    settings: SystemSettings,
    *,
    now: datetime = _NOW,
    at: tuple[float, float] = _ORIGIN,
) -> AllocationResult:
    return await next_batch(
        db, route=route, now=now, lat=at[0], lng=at[1], settings=settings, osrm=FakeOsrmClient()
    )


def _names(stops: Sequence[RouteStop]) -> list[str]:
    return [stop.service_point.name for stop in stops]


def _batch(route: Route, number: int) -> list[RouteStop]:
    return [stop for stop in route.stops if stop.batch_number == number]


async def _finish(db: AsyncSession, stops: Sequence[RouteStop]) -> None:
    for stop in stops:
        stop.status = RouteStopStatus.DONE
    await db.flush()


async def _route_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(Route)) or 0)


# --- the batch -------------------------------------------------------------------------------


async def test_start_allocates_batch_of_configured_size_nearest_first(
    db_session: AsyncSession,
) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco B",
        (-7.2150, -35.9050),
        [("Térreo", "B1"), ("1º andar", "B2")],
        own.service_type,
    )
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A2"), ("Térreo", "A1")],
        own.service_type,
    )
    await _building(db_session, "Bloco C", (-7.2100, -35.9000), [("Térreo", "C1")], None)
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=3)

    result = await _start(db_session, worker, settings)

    route = result.route
    assert result.pool_empty is False
    # Bloco A is nearest; Bloco B's points are equidistant, so the floor label breaks the tie.
    assert _names(route.stops) == ["A1", "A2", "B2"]
    assert [stop.order_index for stop in route.stops] == [1, 2, 3]
    assert {stop.batch_number for stop in route.stops} == {1}
    assert {stop.service_date for stop in route.stops} == {_TODAY}
    assert {stop.status for stop in route.stops} == {RouteStopStatus.PENDING}
    assert {stop.service_type_id for stop in route.stops} == {own.service_type.id}
    assert route.status is RouteStatus.IN_PROGRESS
    assert route.route_date == _TODAY
    assert route.started_at == _NOW
    assert route.last_allocated_at == _NOW
    assert (route.start_latitude, route.start_longitude) == _ORIGIN
    assert route.stops[1].leg_geometry is not None  # legs recomputed over the batch


async def test_only_points_of_the_worker_company_types(db_session: AsyncSession) -> None:
    own = await _company(db_session, "Limpa Tudo")
    other = await _company(db_session, "Jardins SA")
    await _building(
        db_session, "Bloco A", (-7.2150, -35.9095), [("Térreo", "Jardim")], other.service_type
    )
    await _building(
        db_session, "Bloco B", (-7.2150, -35.9050), [("Térreo", "Sala")], own.service_type
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session)

    result = await _start(db_session, worker, settings)

    assert _names(result.route.stops) == ["Sala"]


async def test_untyped_points_and_orphan_types_never_allocated(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    orphan_type = ServiceType(name="Sem empresa", average_duration_minutes=10)
    db_session.add(orphan_type)
    await db_session.flush()
    await _building(db_session, "Bloco A", (-7.2150, -35.9095), [("Térreo", "Sem tipo")], None)
    await _building(db_session, "Bloco B", (-7.2150, -35.9090), [("Térreo", "Órfão")], orphan_type)
    await _building(
        db_session, "Bloco C", (-7.2150, -35.9050), [("Térreo", "Válido")], own.service_type
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session)

    result = await _start(db_session, worker, settings)

    assert _names(result.route.stops) == ["Válido"]


async def test_batch_buildings_follow_the_osrm_trip_order(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session, "Bloco A", (-7.2150, -35.9095), [("Térreo", "A1")], own.service_type
    )
    await _building(
        db_session, "Bloco B", (-7.2150, -35.9050), [("Térreo", "B1")], own.service_type
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session)
    # Waypoint 0 is the worker; OSRM says walk to Bloco B (2) before Bloco A (1).
    osrm = FakeOsrmClient(order=[0, 2, 1])

    result = await _start(db_session, worker, settings, osrm=osrm)

    assert _names(result.route.stops) == ["B1", "A1"]
    assert osrm.calls[0] == (
        "optimize_order",
        [(-35.9100, -7.2150), (-35.9095, -7.2150), (-35.9050, -7.2150)],
    )


async def test_osrm_down_falls_back_to_distance_order(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session, "Bloco B", (-7.2150, -35.9050), [("Térreo", "B1")], own.service_type
    )
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session)

    result = await _start(db_session, worker, settings, osrm=FakeOsrmClient(unavailable=True))

    assert _names(result.route.stops) == ["A1", "A2", "B1"]
    assert all(stop.leg_geometry is None for stop in result.route.stops)


async def test_without_an_osrm_client_the_batch_keeps_distance_order(
    db_session: AsyncSession,
) -> None:
    own = await _company(db_session)
    await _building(
        db_session, "Bloco B", (-7.2150, -35.9050), [("Térreo", "B1")], own.service_type
    )
    await _building(
        db_session, "Bloco A", (-7.2150, -35.9095), [("Térreo", "A1")], own.service_type
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session)

    result = await start_or_resume(
        db_session,
        worker=worker,
        today=_TODAY,
        now=_NOW,
        lat=_ORIGIN[0],
        lng=_ORIGIN[1],
        settings=settings,
        osrm=None,
    )

    assert _names(result.route.stops) == ["A1", "B1"]
    assert all(stop.leg_geometry is None for stop in result.route.stops)


# --- exclusivity -----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def own_sessions(
    db_session: AsyncSession, test_settings: Settings
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on their own engine — their own connections — so two allocations really run
    concurrently. Depends on `db_session` so its truncation runs after this engine is gone."""
    engine = build_engine(test_settings.database_url)
    yield build_session_factory(engine)
    await engine.dispose()


async def _two_workers_one_building(db: AsyncSession) -> tuple[UUID, UUID, set[UUID]]:
    own = await _company(db)
    rooms = [("Térreo", f"Sala {i}") for i in range(1, 7)]
    points = await _building(db, "Bloco CI", (-7.2150, -35.9095), rooms, own.service_type)
    first = await make_worker(db, company=own.company, full_name="Ana")
    second = await make_worker(db, company=own.company, full_name="Bruno")
    await _settings(db, batch_size=4)
    await db.commit()
    return first.id, second.id, {point.id for point in points}


async def _start_in(session: AsyncSession, worker_id: UUID) -> list[UUID]:
    worker = await session.get_one(FieldWorker, worker_id)
    result = await _start(session, worker, await get_settings_row(session))
    return [stop.service_point_id for stop in result.route.stops]


async def test_two_workers_same_building_get_disjoint_points(
    db_session: AsyncSession, own_sessions: async_sessionmaker[AsyncSession]
) -> None:
    first_id, second_id, all_points = await _two_workers_one_building(db_session)

    async def start_and_commit(worker_id: UUID) -> list[UUID]:
        async with own_sessions() as session:
            claimed = await _start_in(session, worker_id)
            await session.commit()
            return claimed

    first, second = await asyncio.gather(start_and_commit(first_id), start_and_commit(second_id))

    # Who wins which point depends on timing (4 + 2 or 3 + 3); what never varies is that no
    # point is handed out twice, nobody is left without work, and the batch size holds.
    assert not set(first) & set(second)
    assert set(first) | set(second) == all_points
    assert max(len(first), len(second)) <= 4


async def test_open_allocation_holds_its_points_against_a_concurrent_claim(
    db_session: AsyncSession, own_sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The first claim is flushed but not committed — its row locks are held — when the second
    claim runs. SKIP LOCKED makes the second skip those rows instead of waiting on them."""
    first_id, second_id, all_points = await _two_workers_one_building(db_session)

    async with own_sessions() as first_session, own_sessions() as second_session:
        first = await _start_in(first_session, first_id)
        second = await asyncio.wait_for(_start_in(second_session, second_id), timeout=10)
        await first_session.commit()
        await second_session.commit()

    assert (len(first), len(second)) == (4, 2)
    assert set(first) | set(second) == all_points


async def test_point_taken_after_the_pool_was_read_is_skipped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pool read just before another worker's claim commits still lists the taken point; the
    unique index refuses its stop and the claim moves on to the next candidate."""
    own = await _company(db_session)
    points = await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    rival = await make_worker(db_session, company=own.company, full_name="Ana")
    worker = await make_worker(db_session, company=own.company, full_name="Bruno")
    settings = await _settings(db_session, batch_size=1)
    await _start(db_session, rival, settings)  # takes A1

    async def stale_pool(*_args: object, **_kwargs: object) -> list[ServicePoint]:
        return points

    monkeypatch.setattr(allocation, "free_points", stale_pool)
    result = await _start(db_session, worker, settings)

    assert _names(result.route.stops) == ["A2"]


# --- batches over the day --------------------------------------------------------------------


async def test_start_twice_returns_same_batch(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", f"A{i}") for i in range(4)],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=2)

    first = await _start(db_session, worker, settings)
    first_stop_ids = [stop.id for stop in first.route.stops]
    again = await _start(db_session, worker, settings)

    assert again.route.id == first.route.id
    assert [stop.id for stop in again.route.stops] == first_stop_ids
    assert await _route_count(db_session) == 1


async def test_next_batch_returns_current_while_batch_live(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", f"A{i}") for i in range(4)],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=2)
    started = await _start(db_session, worker, settings)
    started.route.stops[0].status = RouteStopStatus.DONE
    started.route.stops[1].status = RouteStopStatus.IN_PROGRESS
    await db_session.flush()

    result = await _next(db_session, started.route, settings)

    assert result.pool_empty is False
    assert [stop.batch_number for stop in result.route.stops] == [1, 1]


async def test_next_batch_after_done_allocates_from_new_origin(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    await _building(
        db_session,
        "Bloco B",
        (-7.2150, -35.9070),
        [("Térreo", "B1"), ("Térreo", "B2")],
        own.service_type,
    )
    await _building(
        db_session,
        "Bloco C",
        (-7.2080, -35.9000),
        [("Térreo", "C1"), ("Térreo", "C2")],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=2)
    started = await _start(db_session, worker, settings)
    await _finish(db_session, started.route.stops)
    later = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)

    # Bloco B is nearer the morning origin; the worker is now next to Bloco C.
    result = await _next(db_session, started.route, settings, now=later, at=(-7.2080, -35.9001))

    assert _names(_batch(result.route, 2)) == ["C1", "C2"]
    assert [stop.order_index for stop in _batch(result.route, 2)] == [3, 4]
    assert result.route.last_allocated_at == later
    assert result.route.started_at == _NOW  # set once, by the first batch


async def test_empty_pool_marks_route_done(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=4)
    started = await _start(db_session, worker, settings)
    await _finish(db_session, started.route.stops)

    result = await _next(db_session, started.route, settings)

    assert result.pool_empty is True
    assert result.route.status is RouteStatus.DONE
    assert len(result.route.stops) == 2
    with pytest.raises(allocation.RouteAlreadyDone):
        await _start(db_session, worker, settings)


async def test_paused_route_resumes_with_new_batch_same_route(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=2)
    started = await _start(db_session, worker, settings)
    for stop in started.route.stops:  # what B4's "left campus" release leaves behind
        stop.status = RouteStopStatus.RELEASED
        stop.release_reason = ReleaseReason.LEFT_CAMPUS
    started.route.status = RouteStatus.PAUSED
    await db_session.flush()

    result = await _start(db_session, worker, settings)

    assert result.route.id == started.route.id
    assert result.route.status is RouteStatus.IN_PROGRESS
    # Released points are back in the pool, for this worker too.
    assert _names(_batch(result.route, 2)) == ["A1", "A2"]
    assert await _route_count(db_session) == 1


async def test_point_impeded_by_worker_not_offered_back_to_them(
    db_session: AsyncSession,
) -> None:
    own = await _company(db_session)
    await _building(
        db_session, "Bloco A", (-7.2150, -35.9095), [("Térreo", "A1")], own.service_type
    )
    await _building(
        db_session, "Bloco B", (-7.2150, -35.9050), [("Térreo", "B1")], own.service_type
    )
    worker = await make_worker(db_session, company=own.company, full_name="Ana")
    colleague = await make_worker(db_session, company=own.company, full_name="Bruno")
    settings = await _settings(db_session, batch_size=1)
    started = await _start(db_session, worker, settings)
    impeded = started.route.stops[0]
    db_session.add(
        Impediment(
            route_id=started.route.id,
            route_stop_id=impeded.id,
            scope=ImpedimentScope.STOP,
            reason=ImpedimentReason.ACCESS_BLOCKED,
            reported_by=worker.id,
            reported_at=_NOW,
        )
    )
    impeded.status = RouteStopStatus.RELEASED
    impeded.release_reason = ReleaseReason.IMPEDIMENT
    await db_session.flush()

    own_next = await _next(db_session, started.route, settings)
    colleague_start = await _start(db_session, colleague, settings)

    assert _names(_batch(own_next.route, 2)) == ["B1"]  # A1 is nearer, but she impeded it
    assert _names(colleague_start.route.stops) == ["A1"]


async def test_next_batch_on_a_past_days_route_is_done(db_session: AsyncSession) -> None:
    own = await _company(db_session)
    await _building(
        db_session,
        "Bloco A",
        (-7.2150, -35.9095),
        [("Térreo", "A1"), ("Térreo", "A2")],
        own.service_type,
    )
    worker = await make_worker(db_session, company=own.company)
    settings = await _settings(db_session, batch_size=1)
    started = await _start(db_session, worker, settings)
    await _finish(db_session, started.route.stops)
    next_morning = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)

    # Yesterday's route never hands out today's points; today has a route of its own.
    with pytest.raises(allocation.RouteAlreadyDone):
        await _next(db_session, started.route, settings, now=next_morning)


# --- refusals --------------------------------------------------------------------------------


async def _one_point_world(db: AsyncSession) -> tuple[FieldWorker, SystemSettings]:
    own = await _company(db)
    await _building(db, "Bloco A", (-7.2150, -35.9095), [("Térreo", "A1")], own.service_type)
    return await make_worker(db, company=own.company), await _settings(db)


async def test_saturday_refused(db_session: AsyncSession) -> None:
    worker, settings = await _one_point_world(db_session)

    with pytest.raises(NotAServiceDay):
        await _start(db_session, worker, settings, now=_SATURDAY_NOW)

    assert await _route_count(db_session) == 0


async def test_after_workday_end_refused(db_session: AsyncSession) -> None:
    worker, settings = await _one_point_world(db_session)

    with pytest.raises(WorkdayOver):
        await _start(db_session, worker, settings, now=_FIVE_PM)
    assert await _route_count(db_session) == 0

    started = await _start(db_session, worker, settings)
    await _finish(db_session, started.route.stops)
    with pytest.raises(WorkdayOver):
        await _next(db_session, started.route, settings, now=_FIVE_PM)
    assert started.route.status is RouteStatus.IN_PROGRESS
    assert len(started.route.stops) == 1


async def test_outside_campus_refused(db_session: AsyncSession) -> None:
    worker, settings = await _one_point_world(db_session)

    with pytest.raises(OutsideCampus):
        await _start(db_session, worker, settings, at=(-7.2300, -35.9100))

    assert await _route_count(db_session) == 0
