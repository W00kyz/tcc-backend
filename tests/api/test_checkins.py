import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.core.security import hash_password
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStop
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_started_route(
    db_session: AsyncSession, settings: Settings
) -> tuple[RouteStop, QrCode]:
    worker_user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
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
    worker = FieldWorker(full_name="João", contractor_company_id=company.id, user_id=worker_user.id)
    db_session.add_all([point, worker])
    await db_session.flush()

    qr_payload = sign_qr_payload(
        floor_id=floor.id, version=1, private_key_hex=settings.qr_signing_private_key_hex
    )
    qr_code = QrCode(floor_id=floor.id, public_code=qr_payload, secret=b"sig", version=1)
    route = Route(
        field_worker_id=worker.id,
        route_date=datetime.now(UTC).date(),
        started_at=datetime.now(UTC),
    )
    db_session.add_all([qr_code, route])
    await db_session.flush()

    stop = RouteStop(route_id=route.id, service_point_id=point.id, order_index=1)
    db_session.add(stop)
    await db_session.commit()
    return stop, qr_code


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/login", json={"email": "joao@empresa.com", "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


@pytest.mark.xfail(reason="endpoint rewrite is Task 5", strict=False)
async def test_check_in_with_a_real_signed_qr_succeeds(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    stop, qr_code = await _seed_started_route(db_session, test_settings)
    token = _login(client)

    response = client.post(
        "/check-ins",
        json={
            "route_stop_id": str(stop.id),
            "qr_payload": qr_code.public_code,
            "latitude": -7.2,
            "longitude": -35.9,
            "scanned_at": datetime.now(UTC).isoformat(),
            "idempotency_key": "11111111-1111-1111-1111-111111111111",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    assert response.json()["execution_id"] is not None


async def test_check_in_with_a_forged_qr_is_rejected(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    stop, _qr_code = await _seed_started_route(db_session, test_settings)
    token = _login(client)
    forged = sign_qr_payload(floor_id=uuid.uuid4(), version=1, private_key_hex="99" * 32)

    response = client.post(
        "/check-ins",
        json={
            "route_stop_id": str(stop.id),
            "qr_payload": forged,
            "latitude": -7.2,
            "longitude": -35.9,
            "scanned_at": datetime.now(UTC).isoformat(),
            "idempotency_key": "22222222-2222-2222-2222-222222222222",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
