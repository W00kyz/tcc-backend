from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_manager_creates_edits_and_lists_a_contractor_company(
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
        "/contractor-companies",
        json={"name": "Limpa Tudo Terceirizados Ltda", "cnpj": "12345678000199"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201
    company_id = create.json()["id"]

    edit = client.patch(
        f"/contractor-companies/{company_id}",
        json={"name": "Limpa Tudo Serviços Ltda", "cnpj": "12345678000199"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert edit.status_code == 200
    assert edit.json()["name"] == "Limpa Tudo Serviços Ltda"

    listing = client.get("/contractor-companies", headers={"Authorization": f"Bearer {token}"})
    assert listing.status_code == 200
    assert any(item["id"] == company_id for item in listing.json())
