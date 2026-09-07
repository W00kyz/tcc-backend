"""POST /worker-positions — the field worker's periodic GPS report (Etapa 9, spec §2 Ruling 5)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.config import Settings
from app.core.security import hash_password
from app.domain.catalog.models import ContractorCompany, FieldWorker
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import WorkerPosition
from app.domain.routing.models import Route, RouteStatus
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PASSWORD = "senha-forte-o-suficiente"
_LAT = -7.21
_LNG = -35.90


@dataclass
class SeededWorker:
    worker: FieldWorker
    route: Route


async def _seed(
    db_session: AsyncSession,
    *,
    email: str = "joao@empresa.com",
    status: RouteStatus = RouteStatus.IN_PROGRESS,
    route_worker_email: str | None = None,
) -> SeededWorker:
    """One field worker with a route in `status`. `route_worker_email` points the route at a
    different worker when set (the caller is still `email`)."""
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    user = User(
        name=email.split("@")[0],
        email=email,
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add_all([company, user])
    await db_session.flush()
    worker = FieldWorker(full_name=email, contractor_company_id=company.id, user_id=user.id)
    db_session.add(worker)
    await db_session.flush()

    route_worker_id = worker.id
    if route_worker_email is not None:
        other_user = User(
            name=route_worker_email.split("@")[0],
            email=route_worker_email,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.FIELD_WORKER,
        )
        db_session.add(other_user)
        await db_session.flush()
        other = FieldWorker(
            full_name=route_worker_email,
            contractor_company_id=company.id,
            user_id=other_user.id,
        )
        db_session.add(other)
        await db_session.flush()
        route_worker_id = other.id

    route = Route(field_worker_id=route_worker_id, route_date=date.today(), status=status)
    db_session.add(route)
    await db_session.commit()
    return SeededWorker(worker=worker, route=route)


async def _add_manager(db_session: AsyncSession, email: str) -> None:
    db_session.add(
        User(
            name=email.split("@")[0],
            email=email,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    await db_session.commit()


def _login(client: TestClient, email: str = "joao@empresa.com") -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _post(client: TestClient, token: str, **body: object) -> Response:
    payload: dict[str, object] = {
        "latitude": _LAT,
        "longitude": _LNG,
        "reported_at": datetime.now(UTC).isoformat(),
    }
    payload.update(body)
    return client.post(
        "/worker-positions", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


async def test_in_progress_route_records_position_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session)
    token = _login(client)

    response = _post(client, token, route_id=str(seeded.route.id), accuracy_m=8.0)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["route_id"] == str(seeded.route.id)
    assert body["latitude"] == _LAT
    assert body["accuracy_m"] == 8.0
    assert body["received_at"] is not None

    rows = (await db_session.execute(select(WorkerPosition))).scalars().all()
    assert len(rows) == 1
    assert rows[0].field_worker_id == seeded.worker.id


async def test_route_of_a_different_worker_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session, route_worker_email="maria@empresa.com")
    token = _login(client)

    response = _post(client, token, route_id=str(seeded.route.id))

    assert response.status_code == 409, response.text


async def test_planned_route_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session, status=RouteStatus.PLANNED)
    token = _login(client)

    response = _post(client, token, route_id=str(seeded.route.id))

    assert response.status_code == 409, response.text


async def test_done_route_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session, status=RouteStatus.DONE)
    token = _login(client)

    response = _post(client, token, route_id=str(seeded.route.id))

    assert response.status_code == 409, response.text


async def test_manager_token_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session)
    await _add_manager(db_session, "gerente@empresa.com")
    token = _login(client, email="gerente@empresa.com")

    response = _post(client, token, route_id=str(seeded.route.id))

    assert response.status_code == 403, response.text


async def test_future_reported_at_accepted_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session)
    token = _login(client)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    response = _post(client, token, route_id=str(seeded.route.id), reported_at=future)

    assert response.status_code == 201, response.text


async def test_accuracy_omitted_stored_null_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed(db_session)
    token = _login(client)

    response = _post(client, token, route_id=str(seeded.route.id))

    assert response.status_code == 201, response.text
    assert response.json()["accuracy_m"] is None
    row = (await db_session.execute(select(WorkerPosition))).scalar_one()
    assert row.accuracy_m is None
