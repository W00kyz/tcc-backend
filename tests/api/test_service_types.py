from app.core.security import hash_password
from app.domain.catalog.models import ContractorCompany, ServiceType
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def _seed_user(db_session: AsyncSession, role: UserRole = UserRole.MANAGER) -> User:
    user = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _seed_types(db_session: AsyncSession) -> ContractorCompany:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    db_session.add(company)
    await db_session.flush()
    db_session.add_all(
        [
            ServiceType(
                name="Limpeza", average_duration_minutes=30, contractor_company_id=company.id
            ),
            ServiceType(name="Jardinagem", average_duration_minutes=45),
        ]
    )
    await db_session.commit()
    return company


async def test_list_shows_the_company_of_each_type(
    client: TestClient, db_session: AsyncSession
) -> None:
    company = await _seed_types(db_session)
    token = _login(client, (await _seed_user(db_session)).email)

    response = client.get("/service-types", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert {
        item["name"]: (item["contractor_company_id"], item["contractor_company_name"])
        for item in response.json()
    } == {"Limpeza": (str(company.id), "Limpa Tudo"), "Jardinagem": (None, None)}


async def test_unassigned_filter_lists_only_types_without_a_company(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_types(db_session)
    token = _login(client, (await _seed_user(db_session)).email)

    response = client.get(
        "/service-types", params={"unassigned": True}, headers={"Authorization": f"Bearer {token}"}
    )

    assert [item["name"] for item in response.json()] == ["Jardinagem"]


async def test_types_cannot_be_written_outside_a_company(
    client: TestClient, db_session: AsyncSession
) -> None:
    token = _login(client, (await _seed_user(db_session)).email)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/service-types", json={}, headers=headers).status_code == 405
    assert client.patch("/service-types/x", json={}, headers=headers).status_code == 404


async def test_a_field_worker_cannot_list_service_types(
    client: TestClient, db_session: AsyncSession
) -> None:
    token = _login(client, (await _seed_user(db_session, UserRole.FIELD_WORKER)).email)

    response = client.get("/service-types", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
