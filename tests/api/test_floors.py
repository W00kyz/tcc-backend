from app.core.security import hash_password
from app.domain.catalog.models import Building
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_manager_creates_and_filters_floors_by_building(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building_a = Building(name="Bloco CI", campus_area="CCT")
    building_b = Building(name="Bloco CE", campus_area="CCT")
    db_session.add_all([manager, building_a, building_b])
    await db_session.commit()
    token = _login(client, manager.email)

    create = client.post(
        "/floors",
        json={"building_id": str(building_a.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201

    client.post(
        "/floors",
        json={"building_id": str(building_b.id), "label": "1º Andar"},
        headers={"Authorization": f"Bearer {token}"},
    )

    filtered = client.get(
        f"/floors?building_id={building_a.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["label"] == "Térreo"


async def test_duplicate_floor_label_in_the_same_building_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, building])
    await db_session.commit()
    token = _login(client, manager.email)
    client.post(
        "/floors",
        json={"building_id": str(building.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.post(
        "/floors",
        json={"building_id": str(building.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
