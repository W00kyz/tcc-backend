from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

OUTLINE = {
    "type": "Polygon",
    "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]],
}


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_manager_creates_edits_and_lists_a_building(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    token = _login(client, manager.email)

    create = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT", "footprint": OUTLINE},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201
    # Drawn on the dashboard map: no OSM way behind it.
    assert create.json()["footprint"] == OUTLINE
    assert create.json()["footprint_source"] == "drawn"
    building_id = create.json()["id"]

    edit = client.patch(
        f"/buildings/{building_id}",
        json={"name": "Bloco CI - Informática", "campus_area": "CCT"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert edit.status_code == 200

    listing = client.get("/buildings", headers={"Authorization": f"Bearer {token}"})
    assert any(item["id"] == building_id for item in listing.json())


async def test_a_building_cannot_be_registered_without_its_outline(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Every building is registered with its geometry: it is drawn on the map first."""
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
