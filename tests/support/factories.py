"""Reusable seeding helpers for the execution/evidence tests (Etapa 5, Tasks 2-10).

Extracted from the chain inlined in tests/api/test_routes.py so every check-in / check-out /
evidence test starts from the same minimal graph instead of copy-pasting it."""

from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy.ext.asyncio import AsyncSession

_PASSWORD = "senha-forte-o-suficiente"


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

    route = Route(
        field_worker_id=worker.id,
        route_date=datetime.now(UTC).date(),
        status=RouteStatus.IN_PROGRESS,
        started_at=datetime.now(UTC),
    )
    db_session.add(route)
    await db_session.flush()

    stop = RouteStop(
        route_id=route.id,
        service_point_id=point.id,
        order_index=1,
        status=RouteStopStatus.PENDING,
    )
    db_session.add(stop)
    await db_session.commit()
    return stop, worker
