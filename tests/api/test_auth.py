import uuid

import pytest
from app.core.config import Settings
from app.core.jwt import create_access_token, create_refresh_token
from app.core.security import hash_password
from app.domain.identity.models import AuthLog, AuthLogEvent, User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_manager(db_session: AsyncSession) -> User:
    user = User(
        name="Larissa Almeida",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_login_with_correct_credentials_returns_tokens(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_manager(db_session)

    response = client.post(
        "/auth/login",
        json={"email": "larissa@pu.ufcg.edu.br", "password": "senha-forte-o-suficiente"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


async def test_login_with_wrong_password_returns_401_and_logs_the_attempt(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_manager(db_session)

    response = client.post(
        "/auth/login", json={"email": "larissa@pu.ufcg.edu.br", "password": "senha-errada"}
    )

    assert response.status_code == 401
    logs = (await db_session.scalars(select(AuthLog))).all()
    assert any(log.event == AuthLogEvent.LOGIN_FAILURE for log in logs)


async def test_login_with_unknown_email_returns_401(client: TestClient) -> None:
    response = client.post(
        "/auth/login", json={"email": "ninguem@pu.ufcg.edu.br", "password": "qualquer-coisa"}
    )

    assert response.status_code == 401


async def test_refresh_issues_a_new_access_token(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_manager(db_session)
    login = client.post(
        "/auth/login",
        json={"email": "larissa@pu.ufcg.edu.br", "password": "senha-forte-o-suficiente"},
    )

    response = client.post("/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})

    assert response.status_code == 200
    assert "access_token" in response.json()


async def test_refresh_rejects_an_access_token(client: TestClient, test_settings: Settings) -> None:
    # An access token replayed at the refresh boundary must be rejected by its type claim.
    access_token = create_access_token(
        user_id=uuid.uuid4(), role=UserRole.MANAGER, secret=test_settings.jwt_secret_key, minutes=15
    )

    response = client.post("/auth/refresh", json={"refresh_token": access_token})

    assert response.status_code == 401


async def test_a_protected_route_rejects_a_refresh_token_used_as_bearer_credential(
    client: TestClient, test_settings: Settings
) -> None:
    # A refresh token replayed as bearer credentials must be rejected by its type claim.
    refresh_token = create_refresh_token(
        user_id=uuid.uuid4(), secret=test_settings.jwt_secret_key, days=7
    )

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {refresh_token}"})

    assert response.status_code == 401


@pytest.mark.xfail(reason="GET /routes route arrives in Task 8", strict=True)
async def test_a_protected_route_rejects_a_request_without_a_token(client: TestClient) -> None:
    response = client.get("/routes")

    assert response.status_code in (401, 403)


@pytest.mark.xfail(reason="GET /routes route arrives in Task 8", strict=True)
async def test_a_manager_only_route_rejects_a_field_worker_token(
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
    login = client.post(
        "/auth/login", json={"email": "joao@empresa.com", "password": "senha-forte-o-suficiente"}
    )

    response = client.get(
        "/routes", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )

    assert response.status_code == 403
