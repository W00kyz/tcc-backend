"""Etapa 7 — the form-builder API: DRAFT mutation, role gate, and publish + audit."""

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_PASSWORD = "senha-forte-o-suficiente"


async def _add_user(db_session: AsyncSession, email: str, role: UserRole) -> None:
    db_session.add(
        User(
            name=email.split("@")[0],
            email=email,
            password_hash=hash_password(_PASSWORD),
            role=role,
        )
    )
    await db_session.commit()


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _service_type_id(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/service-types",
        json={"name": "Limpeza", "average_duration_minutes": 30},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _open_form(client: TestClient, headers: dict[str, str], service_type_id: str) -> str:
    response = client.get(f"/service-types/{service_type_id}/form", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["form_id"])


async def test_get_form_creates_form_with_empty_draft(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    token = _login(client, "gerente@empresa.com")
    headers = _auth(token)
    service_type_id = _service_type_id(client, headers)

    response = client.get(f"/service-types/{service_type_id}/form", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service_type_id"] == service_type_id
    assert body["draft"]["version_number"] == 0
    assert body["draft"]["questions"] == []
    assert body["published"] == []


async def test_builder_add_patch_reorder_delete_cycle(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    headers = _auth(_login(client, "gerente@empresa.com"))
    service_type_id = _service_type_id(client, headers)
    form_id = _open_form(client, headers, service_type_id)

    first = client.post(
        f"/forms/{form_id}/draft/questions",
        json={"prompt": "Sala limpa?", "question_type": "BOOLEAN", "required": True},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert len(first.json()["questions"]) == 1
    key_one = first.json()["questions"][0]["stable_key"]

    second = client.post(
        f"/forms/{form_id}/draft/questions",
        json={"prompt": "Observações", "question_type": "TEXT", "required": False},
        headers=headers,
    )
    key_two = second.json()["questions"][1]["stable_key"]

    patched = client.patch(
        f"/forms/{form_id}/draft/questions/{key_one}",
        json={"prompt": "A sala está limpa?", "question_type": "BOOLEAN", "required": True},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["questions"][0]["prompt"] == "A sala está limpa?"

    reordered = client.put(
        f"/forms/{form_id}/draft/order",
        json={"stable_keys": [key_two, key_one]},
        headers=headers,
    )
    assert reordered.status_code == 200, reordered.text
    assert [q["stable_key"] for q in reordered.json()["questions"]] == [key_two, key_one]

    deleted = client.delete(f"/forms/{form_id}/draft/questions/{key_two}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert [q["stable_key"] for q in deleted.json()["questions"]] == [key_one]


async def test_non_manager_forbidden(client: TestClient, db_session: AsyncSession) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    await _add_user(db_session, "campo@empresa.com", UserRole.FIELD_WORKER)
    manager_headers = _auth(_login(client, "gerente@empresa.com"))
    service_type_id = _service_type_id(client, manager_headers)

    worker_headers = _auth(_login(client, "campo@empresa.com"))
    response = client.get(f"/service-types/{service_type_id}/form", headers=worker_headers)
    assert response.status_code == 403


async def test_publish_empty_draft_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    headers = _auth(_login(client, "gerente@empresa.com"))
    service_type_id = _service_type_id(client, headers)
    form_id = _open_form(client, headers, service_type_id)

    response = client.post(f"/forms/{form_id}/publish", headers=headers)
    assert response.status_code == 422, response.text


async def test_publish_writes_version_and_audit(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    headers = _auth(_login(client, "gerente@empresa.com"))
    service_type_id = _service_type_id(client, headers)
    form_id = _open_form(client, headers, service_type_id)

    client.post(
        f"/forms/{form_id}/draft/questions",
        json={"prompt": "Sala limpa?", "question_type": "BOOLEAN", "required": True},
        headers=headers,
    )
    client.post(
        f"/forms/{form_id}/draft/questions",
        json={
            "prompt": "Nível de sujeira",
            "question_type": "SINGLE_CHOICE",
            "required": True,
            "options": ["baixo", "alto"],
        },
        headers=headers,
    )

    published = client.post(f"/forms/{form_id}/publish", headers=headers)
    assert published.status_code == 201, published.text
    assert published.json()["version_number"] == 1
    assert published.json()["status"] == "PUBLISHED"

    overview = client.get(f"/service-types/{service_type_id}/form", headers=headers).json()
    assert len(overview["published"]) == 1
    assert overview["published"][0]["version_number"] == 1
    assert overview["published"][0]["question_count"] == 2
    assert overview["draft"]["version_number"] == 0
    assert len(overview["draft"]["questions"]) == 2

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "form", AuditTrail.action == "publish_form")
    )
    assert audit_count == 1
