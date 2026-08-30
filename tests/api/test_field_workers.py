import uuid

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


async def test_linking_a_non_field_worker_role_user_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I1: _assert_user_available used to check only that a
    user_id wasn't already claimed by another field worker, never that the User actually has
    role FIELD_WORKER — linking an ADMIN's or MANAGER's user_id used to succeed (201)."""
    manager, company, service_type, _ = await _seed(db_session)
    other_manager = User(
        name="Outro Gestor",
        email="outro-gestor@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(other_manager)
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(other_manager.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_linking_an_unknown_user_id_returns_404(
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
            "user_id": str(uuid.uuid4()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_creating_a_field_worker_for_an_unknown_contractor_company_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.3."""
    manager, _company, service_type, _ = await _seed(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(uuid.uuid4()),
            "service_type_ids": [str(service_type.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_manager_lists_linkable_users(client: TestClient, db_session: AsyncSession) -> None:
    """Finding C2: GET /field-workers/linkable-users returns only FIELD_WORKER-role users not
    yet linked to a FieldWorker, and a Manager (not just an Admin) can call it."""
    manager, company, service_type, unlinked_worker = await _seed(db_session)
    linked_worker = User(
        name="Maria",
        email="maria@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add(linked_worker)
    await db_session.commit()
    token = _login(client, manager.email)
    client.post(
        "/field-workers",
        json={
            "full_name": "Maria Souza",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type.id)],
            "user_id": str(linked_worker.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.get(
        "/field-workers/linkable-users", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert str(unlinked_worker.id) in ids
    assert str(linked_worker.id) not in ids
    assert str(manager.id) not in ids


async def test_patch_response_reflects_a_disjoint_service_type_set(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for a stale-collection bug: the session has expire_on_commit=False, and
    _sync_service_types() mutates FieldWorkerServiceType rows through a separately queried set,
    which never touches a `worker` instance's already-cached `service_type_links` collection
    sitting in the session's identity map. The PATCH response used to echo the pre-update set
    even though the database write itself was correct — only surfaced by moving to a fully
    disjoint set, since every other test here reuses the same single service type across create
    and update."""
    manager, company, service_type_a, _ = await _seed(db_session)
    service_type_b = ServiceType(name="Jardinagem", average_duration_minutes=45)
    service_type_c = ServiceType(name="Manutenção", average_duration_minutes=60)
    db_session.add_all([service_type_b, service_type_c])
    await db_session.commit()
    token = _login(client, manager.email)

    created = client.post(
        "/field-workers",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type_a.id), str(service_type_b.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert sorted(created["service_type_ids"]) == sorted(
        [str(service_type_a.id), str(service_type_b.id)]
    )

    response = client.patch(
        f"/field-workers/{created['id']}",
        json={
            "full_name": "João da Silva",
            "contractor_company_id": str(company.id),
            "service_type_ids": [str(service_type_c.id)],
            "user_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["service_type_ids"] == [str(service_type_c.id)]
