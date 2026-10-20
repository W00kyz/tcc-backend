"""Seeds the one-point walking skeleton (spec deliverable for Etapa 2). Run once per fresh
database: uv run python -m app.seed

Safe to re-run: a database that already has the seeded admin is left untouched (the fixture is
inserted all-or-nothing, so "admin exists" means "fixture exists"). It is still a development
fixture, not a migration — never run it against production data.
"""

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import models_registry  # noqa: F401 — every model, so all FKs resolve on flush
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
from app.domain.qr.crypto import derive_public_key_hex, sign_qr_payload
from app.domain.qr.models import QrCode

_ADMIN_EMAIL = "admin@pu.ufcg.edu.br"

_BLOCO_CI_OUTLINE = {
    "type": "Polygon",
    "coordinates": [
        [
            [-35.9109, -7.2192],
            [-35.9101, -7.2192],
            [-35.9101, -7.2198],
            [-35.9109, -7.2198],
            [-35.9109, -7.2192],
        ]
    ],
}


def _print_logins(qr_private_key_hex: str) -> None:
    print(f"Admin login: {_ADMIN_EMAIL} / trocar-esta-senha")
    print("Manager login: larissa@pu.ufcg.edu.br / trocar-esta-senha")
    print("Field worker login: joao@empresa.com / trocar-esta-senha")
    # The app verifies QR signatures offline with this key (RNF13) — pass it to `flutter run`.
    print(f"QR_PUBLIC_KEY_HEX (app): {derive_public_key_hex(qr_private_key_hex)}")


async def seed() -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    async with session_factory() as db:
        already_seeded = await db.scalar(select(User).where(User.email == _ADMIN_EMAIL))
        if already_seeded is not None:
            print("Database already seeded; nothing to do.")
            _print_logins(settings.qr_signing_private_key_hex)
            await engine.dispose()
            return
        admin = User(
            name="Admin PU",
            email=_ADMIN_EMAIL,
            password_hash=hash_password("trocar-esta-senha"),
            role=UserRole.ADMIN,
        )
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
        building = Building(
            name="Bloco CI",
            campus_area="Centro de Ciências e Tecnologia",
            # Every building is registered with its outline (a rectangle around Sala 101).
            footprint=_BLOCO_CI_OUTLINE,
            footprint_source="drawn",
        )
        db.add_all([admin, manager, worker_user, company, service_type, building])
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
        # No route: the worker gets one from the automatic allocation ("Iniciar rota").
        db.add(qr_code)
        await db.commit()

        print(f"Printed QR (full payload): {qr_code.public_code}")
        _print_logins(settings.qr_signing_private_key_hex)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
