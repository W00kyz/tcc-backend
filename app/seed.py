"""Seeds the one-point walking skeleton (spec deliverable for Etapa 2). Run once per fresh
database: uv run python -m app.seed

Not idempotent by design — this is a development fixture, not a migration. Running it twice
against the same database raises a unique-constraint error on the CNPJ/e-mail, which is the
correct signal that the database already has this fixture.
"""

import asyncio
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import build_engine, build_session_factory
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStop, StopAssignment


async def seed() -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    async with session_factory() as db:
        manager = User(
            name="Larissa Almeida",
            email="larissa@pu.ufcg.edu.br",
            password_hash=hash_password("trocar-esta-senha"),
            role=UserRole.MANAGER,
        )
        worker_user = User(
            name="João da Silva",
            email="joao@empresa.com",
            password_hash=hash_password("trocar-esta-senha"),
            role=UserRole.FIELD_WORKER,
        )
        company = ContractorCompany(name="Limpa Tudo Terceirizados Ltda", cnpj="12345678000199")
        service_type = ServiceType(name="Limpeza", average_duration_minutes=30)
        building = Building(name="Bloco CI", campus_area="Centro de Ciências e Tecnologia")
        db.add_all([manager, worker_user, company, service_type, building])
        await db.flush()

        floor = Floor(building_id=building.id, label="Térreo")
        db.add(floor)
        await db.flush()

        point = ServicePoint(
            floor_id=floor.id,
            name="Sala 101",
            description="Sala de aula",
            latitude=-7.2195,
            longitude=-35.9105,
        )
        worker = FieldWorker(
            full_name="João da Silva", contractor_company_id=company.id, user_id=worker_user.id
        )
        db.add_all([point, worker])
        await db.flush()

        qr_payload = sign_qr_payload(
            floor_id=floor.id, version=1, private_key_hex=settings.qr_signing_private_key_hex
        )
        qr_code = QrCode(floor_id=floor.id, public_code=qr_payload, secret=b"seeded", version=1)
        route = Route(
            field_worker_id=worker.id,
            route_date=datetime.now(UTC).date(),
            scheduled_start_at=datetime.now(UTC),
        )
        db.add_all([qr_code, route])
        await db.flush()

        stop = RouteStop(route_id=route.id, service_point_id=point.id, order_index=1)
        db.add(stop)
        await db.flush()

        db.add(
            StopAssignment(
                route_stop_id=stop.id, field_worker_id=worker.id, sequence=1, assigned_by=manager.id
            )
        )
        await db.commit()

        print(f"Seeded route: route_id={route.id}")
        print(f"Printed QR (full payload): {qr_code.public_code}")
        print("Manager login: larissa@pu.ufcg.edu.br / trocar-esta-senha")
        print("Field worker login: joao@empresa.com / trocar-esta-senha")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
