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


async def _seed(db_session: AsyncSession) -> tuple[User, ContractorCompany, ServiceType, User]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    service_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    worker_login = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add_all([manager, company, service_type, worker_login])
    await db_session.commit()
    return manager, company, service_type, worker_login


async def test_manager_creates_a_field_worker_with_company_and_service_types(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company, service_type, _ = await _seed(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["full_name"] == "João da Silva"
    assert body["service_type_ids"] == [str(service_type.id)]
    assert body["user_id"] is None


async def test_manager_links_a_field_worker_to_a_login_user(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company, service_type, worker_login = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    response = client.patch(
        f"/field-workers/{created['id']}",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(worker_login.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(worker_login.id)


async def test_a_login_user_already_linked_to_another_worker_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company, service_type, worker_login = await _seed(db_session)
    token = _login(client, manager.email)
    client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(worker_login.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.post(
        "/field-workers",
        json={
            "full_name": "Outro Profissional",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(worker_login.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_a_patch_cannot_steal_a_user_already_linked_to_another_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company, service_type, worker_login = await _seed(db_session)
    token = _login(client, manager.email)
    linked = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(worker_login.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    unlinked = client.post(
        "/field-workers",
        json={
            "full_name": "Outro Profissional",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    response = client.patch(
        f"/field-workers/{unlinked['id']}",
        json={
            "full_name": "Outro Profissional",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(worker_login.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    unchanged = client.get("/field-workers", headers={"Authorization": f"Bearer {token}"}).json()
    still_linked = next(item for item in unchanged if item["id"] == linked["id"])
    assert still_linked["user_id"] == str(worker_login.id)
