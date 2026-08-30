from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def _seed_manager(db_session: AsyncSession) -> User:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    return manager


async def test_manager_creates_and_lists_a_service_type(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = await _seed_manager(db_session)
    token = _login(client, manager.email)

    create = client.post(
        "/service-types",
        json={"name": "Limpeza", "average_duration_minutes": 30},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201
    assert create.json()["name"] == "Limpeza"

    listing = client.get("/service-types", headers={"Authorization": f"Bearer {token}"})
    assert listing.status_code == 200
    assert any(item["name"] == "Limpeza" for item in listing.json())


async def test_manager_edits_a_service_type(client: TestClient, db_session: AsyncSession) -> None:
    manager = await _seed_manager(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/service-types",
        json={"name": "Limpeza", "average_duration_minutes": 30},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    response = client.patch(
        f"/service-types/{created['id']}",
        json={"name": "Limpeza Pesada", "average_duration_minutes": 45},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["average_duration_minutes"] == 45


async def test_duplicate_service_type_name_returns_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.4."""
    manager = await _seed_manager(db_session)
    token = _login(client, manager.email)
    client.post(
        "/service-types",
        json={"name": "Limpeza", "average_duration_minutes": 30},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.post(
        "/service-types",
        json={"name": "Limpeza", "average_duration_minutes": 45},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_a_field_worker_cannot_create_a_service_type(
    client: TestClient, db_session: AsyncSession
) -> None:
    worker = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add(worker)
    await db_session.commit()
    token = _login(client, worker.email)

    response = client.post(
        "/service-types",
        json={"name": "X", "average_duration_minutes": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
