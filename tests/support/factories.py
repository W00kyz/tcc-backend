"""Reusable seeding helpers for the execution/evidence tests (Etapa 5, Tasks 2-10).

Extracted from the chain inlined in tests/api/test_routes.py so every check-in / check-out /
evidence test starts from the same minimal graph instead of copy-pasting it."""

from collections.abc import Sequence
from datetime import UTC, date, datetime

from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus, RouteStop
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus

_PASSWORD = "senha-forte-o-suficiente"


async def make_route(
    db: AsyncSession,
    *,
    worker: FieldWorker,
    day: date,
    points: Sequence[ServicePoint],
    status: RouteStatus = RouteStatus.IN_PROGRESS,
) -> Route:
    """A route as the automatic allocation leaves it: started, with one batch (`batch_number=1`)
    of PENDING stops over `points` in that order, each stamped `service_date=day`. Flushes, never
    commits; `route.stops` comes back loaded.

        route = await make_route(db, worker=worker, day=today_on_campus(), points=[point])
        await db.commit()
    """
    now = datetime.now(UTC)
    route = Route(
        field_worker_id=worker.id,
        route_date=day,
        status=status,
        started_at=now,
        last_allocated_at=now,
    )
    db.add(route)
    await db.flush()
    db.add_all(
        RouteStop(
            route_id=route.id,
            service_point_id=point.id,
            order_index=index,
            batch_number=1,
            service_date=day,
        )
        for index, point in enumerate(points, start=1)
    )
    await db.flush()
    await db.refresh(route, attribute_names=["stops"])
    return route


async def seed_route_with_one_stop(
    db_session: AsyncSession,
) -> tuple[RouteStop, FieldWorker]:
    """Seed ContractorCompany -> FieldWorker(+User) -> Building -> Floor -> ServicePoint ->
    Route (already started, IN_PROGRESS) -> RouteStop (PENDING). Returns (stop, field_worker)."""
    worker_user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="Sala", latitude=-7.2, longitude=-35.9
    )
    worker = FieldWorker(
        full_name="João da Silva", contractor_company_id=company.id, user_id=worker_user.id
    )
    db_session.add_all([point, worker])
    await db_session.flush()

    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=[point])
    await db_session.commit()
    return route.stops[0], worker
