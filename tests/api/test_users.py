from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_admin(db_session: AsyncSession) -> User:
    admin = User(
        name="Admin",
        email="admin@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.ADMIN,
    )
    db_session.add(admin)
    await db_session.commit()
    return admin


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_admin_creates_a_user_and_an_invite_email_is_sent(
    client: TestClient, db_session: AsyncSession, recording_mailer: RecordingMailer
) -> None:
    await _seed_admin(db_session)
    token = _login(client, "admin@pu.ufcg.edu.br")

    response = client.post(
        "/users",
        json={
            "name": "Larissa Almeida",
            "email": "larissa@pu.ufcg.edu.br",
            "role": "MANAGER",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "larissa@pu.ufcg.edu.br"
    assert body["role"] == "MANAGER"
    assert body["is_active"] is True
    assert "password_hash" not in body
    assert len(recording_mailer.sent) == 1
    assert recording_mailer.sent[0]["to"] == "larissa@pu.ufcg.edu.br"
    assert "redefinir-senha?token=" in recording_mailer.sent[0]["body"]


async def test_a_field_worker_cannot_create_a_user(
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
    token = _login(client, "joao@empresa.com")

    response = client.post(
        "/users",
        json={"name": "X", "email": "x@pu.ufcg.edu.br", "role": "MANAGER"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


async def test_admin_deactivates_and_reactivates_a_user(
    client: TestClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    token = _login(client, admin.email)

    deactivate = client.post(
        f"/users/{manager.id}/deactivate", headers={"Authorization": f"Bearer {token}"}
    )
    assert deactivate.status_code == 200
    assert deactivate.json()["is_active"] is False

    reactivate = client.post(
        f"/users/{manager.id}/activate", headers={"Authorization": f"Bearer {token}"}
    )
    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True


async def test_admin_edits_a_user(client: TestClient, db_session: AsyncSession) -> None:
    admin = await _seed_admin(db_session)
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    token = _login(client, admin.email)

    response = client.patch(
        f"/users/{manager.id}",
        json={
            "name": "Larissa Costa Almeida",
            "email": "larissa@pu.ufcg.edu.br",
            "role": "MANAGER",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Larissa Costa Almeida"


async def test_list_users(client: TestClient, db_session: AsyncSession) -> None:
    admin = await _seed_admin(db_session)
    token = _login(client, admin.email)

    response = client.get("/users", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    emails = [u["email"] for u in response.json()]
    assert admin.email in emails
