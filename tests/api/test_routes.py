from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStop
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_manager_and_worker_route(
    db_session: AsyncSession,
) -> tuple[User, User, FieldWorker, Route]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    worker_user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="Sala", latitude=-7.2, longitude=-35.9
    )
    # full_name deliberately differs from worker_user.name: field_worker_name must come from
    # FieldWorker.full_name, not User.name — a regression test for that would silently pass if
    # the two strings matched.
    worker = FieldWorker(
        full_name="João da Silva", contractor_company_id=company.id, user_id=worker_user.id
    )
    db_session.add_all([point, worker])
    await db_session.flush()

    route = Route(field_worker_id=worker.id)
    db_session.add(route)
    await db_session.flush()
    db_session.add(RouteStop(route_id=route.id, service_point_id=point.id, order_index=1))
    await db_session.commit()
    return manager, worker_user, worker, route


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_field_worker_sees_only_their_own_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")

    response = client.get("/routes/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(route.id)
    assert len(body[0]["stops"]) == 1
    assert body[0]["field_worker_name"] == worker.full_name


async def test_manager_sees_all_routes(client: TestClient, db_session: AsyncSession) -> None:
    _manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "larissa@pu.ufcg.edu.br")

    response = client.get("/routes", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert any(item["id"] == str(route.id) for item in body)
    matching = next(item for item in body if item["id"] == str(route.id))
    assert matching["field_worker_name"] == worker.full_name


async def test_worker_can_start_their_own_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")

    response = client.post(
        f"/routes/{route.id}/start",
        json={"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["started_at"] is not None
    assert body["field_worker_name"] == worker.full_name


async def test_starting_an_already_started_route_returns_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")
    body = {"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()}
    headers = {"Authorization": f"Bearer {token}"}
    client.post(f"/routes/{route.id}/start", json=body, headers=headers)

    response = client.post(f"/routes/{route.id}/start", json=body, headers=headers)

    assert response.status_code == 409
