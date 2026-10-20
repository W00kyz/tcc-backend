"""Reusable seeding helpers for the execution/evidence tests (Etapa 5, Tasks 2-10).

Extracted from the chain inlined in tests/api/test_routes.py so every check-in / check-out /
evidence test starts from the same minimal graph instead of copy-pasting it."""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.execution.models import (
    Execution,
    ExecutionSource,
    GeoValidation,
    QrScan,
    QrScanKind,
)
from app.domain.identity.models import User, UserRole
from app.domain.privacy.models import PrivacyNotice
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStatus, RouteStop
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus

_PASSWORD = "senha-forte-o-suficiente"
# Inside the default campus rectangle (system_settings.campus_*).
ON_CAMPUS = (-7.2150, -35.9100)


async def seed_privacy_notice(db: AsyncSession, *, version: int = 1) -> PrivacyNotice:
    """The one current privacy notice. The tables are emptied between tests, taking the notice
    the migration seeded with them. Commits.

        await seed_privacy_notice(db_session)
    """
    await db.execute(text("DELETE FROM privacy_notices"))
    notice = PrivacyNotice(
        version=version,
        title="Aviso de Privacidade",
        body_markdown="# Aviso",
        published_at=datetime.now(UTC),
        is_current=True,
    )
    db.add(notice)
    await db.commit()
    return notice


async def make_worker(
    db: AsyncSession,
    *,
    company: ContractorCompany,
    full_name: str = "João da Silva",
    email: str | None = None,
    status: FieldWorkerStatus = FieldWorkerStatus.APPROVED,
    is_active: bool = True,
) -> FieldWorker:
    """A field worker with the login every worker has (log in with `_PASSWORD`). Approved and
    active unless told otherwise; flushes, never commits.

        worker = await make_worker(db, company=company, status=FieldWorkerStatus.REJECTED)
    """
    user = User(
        name=full_name,
        email=email or f"{uuid.uuid4().hex[:12]}@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    db.add(user)
    await db.flush()
    worker = FieldWorker(
        full_name=full_name,
        contractor_company_id=company.id,
        user_id=user.id,
        status=status,
        is_active=is_active,
    )
    db.add(worker)
    await db.flush()
    return worker


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


async def make_service_type(
    db: AsyncSession, *, company_name: str = "Limpa Tudo", type_name: str = "Limpeza"
) -> ServiceType:
    """A contractor company owning one service type — what puts points in its workers' pool.
    Flushes, never commits; `service_type.contractor_company` comes back set.

        service_type = await make_service_type(db, company_name="Verde", type_name="Jardinagem")
    """
    company = ContractorCompany(name=company_name, cnpj=str(uuid.uuid4().int)[:14])
    service_type = ServiceType(
        name=type_name, average_duration_minutes=30, contractor_company=company
    )
    db.add_all([company, service_type])
    await db.flush()
    return service_type


async def make_points(
    db: AsyncSession,
    *,
    service_type: ServiceType | None,
    names: Sequence[str],
    at: tuple[float, float] = ON_CAMPUS,
) -> list[ServicePoint]:
    """One building at `at` (lat, lng) with a single floor holding one point per name, all of
    `service_type`. Flushes, never commits.

        points = await make_points(db, service_type=service_type, names=["Sala 1", "Sala 2"])
    """
    building = Building(name=f"Bloco {uuid.uuid4().hex[:6]}", campus_area="CCT")
    db.add(building)
    await db.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()
    points = [
        ServicePoint(
            floor_id=floor.id,
            name=name,
            description="Sala",
            latitude=at[0],
            longitude=at[1],
            service_type_id=service_type.id if service_type is not None else None,
        )
        for name in names
    ]
    db.add_all(points)
    await db.flush()
    return points


async def make_check_in(
    db: AsyncSession, *, stop: RouteStop, worker: FieldWorker, scanned_at: datetime
) -> Execution:
    """An open app execution on `stop` with its CHECK_IN scan at `scanned_at`, read from a QR
    code of the stop's floor. Leaves the stop's status alone. Flushes, never commits.

        execution = await make_check_in(db, stop=stop, worker=worker, scanned_at=now)
    """
    point = await db.get(ServicePoint, stop.service_point_id)
    assert point is not None
    qr_code = QrCode(
        floor_id=point.floor_id,
        public_code=f"qr-{uuid.uuid4()}",
        secret=b"sig",
        version=1,
    )
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=scanned_at,
        synced_at=scanned_at,
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db.add_all([qr_code, execution])
    await db.flush()
    db.add(
        QrScan(
            execution_id=execution.id,
            qr_code_id=qr_code.id,
            kind=QrScanKind.CHECK_IN,
            scanned_at=scanned_at,
            received_at=scanned_at,
            geo_validation=GeoValidation.VALIDATED,
            service_point_id=point.id,
        )
    )
    await db.flush()
    return execution
