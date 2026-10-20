"""`candidates_for` (Task 4, spec Ruling 3) — ranking the field workers eligible to take over
an impediment's affected stop(s) by PostGIS distance and current load. Runs against the real
Postgres/PostGIS testcontainer (`db_session`) — `ST_DistanceSphere` has no SQLite/mock stand-in.
"""

import uuid
from datetime import UTC, date, datetime

from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerServiceType,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.identity.models import User, UserRole
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
)
from app.domain.impediments.service import candidates_for
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_ROUTE_DATE = date(2026, 9, 1)
_OTHER_DATE = date(2026, 9, 2)
_PASSWORD = "senha-forte-o-suficiente"


class _World:
    """A minimal seeded graph plus builders — every candidate scenario shares the company,
    building and floor, and differs only in workers/routes/stops."""

    def __init__(self, db: AsyncSession, company: ContractorCompany, floor: Floor) -> None:
        self.db = db
        self.company = company
        self.floor = floor
        self._email_seq = 0

    async def service_type(self, name: str) -> ServiceType:
        st = ServiceType(name=name, average_duration_minutes=30)
        self.db.add(st)
        await self.db.flush()
        return st

    async def point(self, *, latitude: float, longitude: float) -> ServicePoint:
        point = ServicePoint(
            floor_id=self.floor.id,
            name=f"Ponto {latitude},{longitude}",
            description="Sala",
            latitude=latitude,
            longitude=longitude,
        )
        self.db.add(point)
        await self.db.flush()
        return point

    async def worker(
        self, *, full_name: str, with_user: bool = True, service_type_ids: list[uuid.UUID]
    ) -> FieldWorker:
        user_id = None
        if with_user:
            self._email_seq += 1
            user = User(
                name=full_name,
                email=f"w{self._email_seq}@empresa.com",
                password_hash=hash_password(_PASSWORD),
                role=UserRole.FIELD_WORKER,
            )
            self.db.add(user)
            await self.db.flush()
            user_id = user.id
        worker = FieldWorker(
            full_name=full_name, contractor_company_id=self.company.id, user_id=user_id
        )
        self.db.add(worker)
        await self.db.flush()
        for st_id in service_type_ids:
            self.db.add(FieldWorkerServiceType(field_worker_id=worker.id, service_type_id=st_id))
        await self.db.flush()
        return worker

    async def route_with_stops(
        self,
        *,
        worker: FieldWorker,
        route_date: date = _ROUTE_DATE,
        status: RouteStatus = RouteStatus.PLANNED,
        stops: list[tuple[ServicePoint, uuid.UUID | None]],
        stop_status: RouteStopStatus = RouteStopStatus.PENDING,
    ) -> Route:
        route = Route(field_worker_id=worker.id, route_date=route_date, status=status)
        self.db.add(route)
        await self.db.flush()
        for index, (point, service_type_id) in enumerate(stops, start=1):
            self.db.add(
                RouteStop(
                    route_id=route.id,
                    service_point_id=point.id,
                    order_index=index,
                    service_type_id=service_type_id,
                    status=stop_status,
                )
            )
        await self.db.flush()
        return route

    async def impediment(
        self,
        *,
        route: Route,
        scope: ImpedimentScope,
        route_stop: RouteStop | None = None,
    ) -> Impediment:
        imp = Impediment(
            route_id=route.id,
            route_stop_id=route_stop.id if route_stop is not None else None,
            scope=scope,
            reason=ImpedimentReason.OTHER,
            note=None,
            reported_by=route.field_worker_id,
            reported_at=datetime.now(UTC),
        )
        self.db.add(imp)
        await self.db.flush()
        return imp


async def _world(db: AsyncSession) -> _World:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([company, building])
    await db.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()
    return _World(db, company, floor)


async def _stops_of(db: AsyncSession, route: Route) -> list[RouteStop]:
    rows = await db.scalars(
        select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.order_index)
    )
    return list(rows)


async def test_candidates_ranked_by_distance_with_eligibility_filters(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    st_b = await world.service_type("Jardinagem")

    affected_point = await world.point(latitude=0.0, longitude=0.0)
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[(affected_point, st_a.id)],
    )
    impeded_stop = (await _stops_of(db_session, impeded_route))[0]
    imp = await world.impediment(
        route=impeded_route, scope=ImpedimentScope.STOP, route_stop=impeded_stop
    )

    # Near / mid / far — only the relative order matters, not the exact Haversine value.
    near_worker = await world.worker(full_name="Perto", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=near_worker, stops=[(await world.point(latitude=0.0, longitude=0.01), None)]
    )
    mid_worker = await world.worker(full_name="Medio", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=mid_worker, stops=[(await world.point(latitude=0.0, longitude=0.02), None)]
    )
    far_worker = await world.worker(full_name="Longe", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=far_worker, stops=[(await world.point(latitude=1.0, longitude=1.0), None)]
    )

    # Compatible but no route on the impediment's date → excluded.
    no_route_worker = await world.worker(full_name="SemRota", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=no_route_worker,
        route_date=_OTHER_DATE,
        stops=[(await world.point(latitude=0.0, longitude=0.005), None)],
    )
    # Route on the date but not qualified for the affected service type → excluded.
    wrong_type_worker = await world.worker(full_name="TipoErrado", service_type_ids=[st_b.id])
    await world.route_with_stops(
        worker=wrong_type_worker,
        stops=[(await world.point(latitude=0.0, longitude=0.005), None)],
    )
    # Compatible + route on the date but no linked user (RF08) → excluded.
    no_user_worker = await world.worker(
        full_name="SemUsuario", with_user=False, service_type_ids=[st_a.id]
    )
    await world.route_with_stops(
        worker=no_user_worker,
        stops=[(await world.point(latitude=0.0, longitude=0.005), None)],
    )

    candidates = await candidates_for(db_session, impediment=imp)

    assert [c.field_worker_id for c in candidates] == [
        near_worker.id,
        mid_worker.id,
        far_worker.id,
    ]
    assert candidates[0].full_name == "Perto"
    assert candidates[0].distance_m < candidates[1].distance_m < candidates[2].distance_m
    assert all(c.pending_stop_count == 1 for c in candidates)


async def test_candidates_tiebreak_prefers_lighter_load(db_session: AsyncSession) -> None:
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    affected_point = await world.point(latitude=0.0, longitude=0.0)
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[(affected_point, st_a.id)],
    )
    impeded_stop = (await _stops_of(db_session, impeded_route))[0]
    imp = await world.impediment(
        route=impeded_route, scope=ImpedimentScope.STOP, route_stop=impeded_stop
    )

    # Both candidates have a pending stop at the SAME coordinates → identical distance; the
    # one with fewer pending stops must rank first.
    heavy = await world.worker(full_name="Cheio", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=heavy,
        stops=[
            (await world.point(latitude=0.0, longitude=0.03), None),
            (await world.point(latitude=0.0, longitude=0.9), None),
        ],
    )
    light = await world.worker(full_name="Leve", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=light,
        stops=[(await world.point(latitude=0.0, longitude=0.03), None)],
    )

    candidates = await candidates_for(db_session, impediment=imp)

    assert [c.field_worker_id for c in candidates] == [light.id, heavy.id]
    assert candidates[0].distance_m == candidates[1].distance_m
    assert (candidates[0].pending_stop_count, candidates[1].pending_stop_count) == (1, 2)


async def test_candidates_count_in_progress_stop_as_load(db_session: AsyncSession) -> None:
    # F1 fix: an IN_PROGRESS stop on a candidate's own route is still load they carry, so it
    # counts toward pending_stop_count and the tiebreak.
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    affected_point = await world.point(latitude=0.0, longitude=0.0)
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[(affected_point, st_a.id)],
    )
    impeded_stop = (await _stops_of(db_session, impeded_route))[0]
    imp = await world.impediment(
        route=impeded_route, scope=ImpedimentScope.STOP, route_stop=impeded_stop
    )

    busy = await world.worker(full_name="Ocupado", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=busy,
        status=RouteStatus.IN_PROGRESS,
        stops=[(await world.point(latitude=0.0, longitude=0.03), None)],
        stop_status=RouteStopStatus.IN_PROGRESS,
    )

    candidates = await candidates_for(db_session, impediment=imp)

    assert [c.field_worker_id for c in candidates] == [busy.id]
    assert candidates[0].pending_stop_count == 1


async def test_candidates_route_remainder_requires_every_service_type(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    st_b = await world.service_type("Jardinagem")
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id, st_b.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[
            (await world.point(latitude=0.0, longitude=0.0), st_a.id),
            (await world.point(latitude=0.0, longitude=0.001), st_b.id),
        ],
    )
    imp = await world.impediment(route=impeded_route, scope=ImpedimentScope.ROUTE_REMAINDER)

    only_a = await world.worker(full_name="SoLimpeza", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=only_a, stops=[(await world.point(latitude=0.0, longitude=0.01), None)]
    )
    both = await world.worker(full_name="Ambos", service_type_ids=[st_a.id, st_b.id])
    await world.route_with_stops(
        worker=both, stops=[(await world.point(latitude=0.0, longitude=0.02), None)]
    )

    candidates = await candidates_for(db_session, impediment=imp)

    assert [c.field_worker_id for c in candidates] == [both.id]


async def test_candidates_empty_when_route_remainder_has_no_pending(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[(await world.point(latitude=0.0, longitude=0.0), st_a.id)],
        stop_status=RouteStopStatus.DONE,
    )
    imp = await world.impediment(route=impeded_route, scope=ImpedimentScope.ROUTE_REMAINDER)

    assert await candidates_for(db_session, impediment=imp) == []


async def test_candidates_never_include_the_impeded_worker_second_route(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    st_a = await world.service_type("Limpeza")
    impeded_worker = await world.worker(full_name="Impedido", service_type_ids=[st_a.id])
    impeded_route = await world.route_with_stops(
        worker=impeded_worker,
        status=RouteStatus.IN_PROGRESS,
        stops=[(await world.point(latitude=0.0, longitude=0.0), st_a.id)],
    )
    # Same worker, same day, a second (e.g. OCCASIONAL) route still PLANNED.
    await world.route_with_stops(
        worker=impeded_worker,
        stops=[(await world.point(latitude=0.0, longitude=0.01), None)],
    )
    other = await world.worker(full_name="Substituta", service_type_ids=[st_a.id])
    await world.route_with_stops(
        worker=other, stops=[(await world.point(latitude=0.0, longitude=0.02), None)]
    )
    impeded_stop = (await _stops_of(db_session, impeded_route))[0]
    imp = await world.impediment(
        route=impeded_route, scope=ImpedimentScope.STOP, route_stop=impeded_stop
    )

    candidates = await candidates_for(db_session, impediment=imp)

    assert [c.field_worker_id for c in candidates] == [other.id]
