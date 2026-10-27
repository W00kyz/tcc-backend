"""Etapa 11 (RNF10): the LGPD consent gate — `consent_required` on the login and refresh
responses and the `/auth/consent` + `/auth/consent-status` routes."""

from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from app.domain.privacy.models import PrivacyNotice
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PASSWORD = "senha-forte-o-suficiente"


async def _seed(db_session: AsyncSession) -> User:
    # Truncation between tests may have removed the migration-seeded v1 notice.
    await db_session.execute(text("DELETE FROM privacy_notices"))
    db_session.add(
        PrivacyNotice(
            version=1,
            title="Aviso de Privacidade",
            body_markdown="# Aviso\nTratamos seus dados conforme a LGPD.",
            published_at=datetime.now(UTC),
            is_current=True,
        )
    )
    worker = User(
        name="Joana Field",
        email="joana@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add(worker)
    await db_session.commit()
    return worker


async def test_login_reports_consent_required_then_false_after_consent(
    client: TestClient, db_session: AsyncSession
) -> None:
    worker = await _seed(db_session)

    r = client.post("/auth/login", json={"email": worker.email, "password": _PASSWORD})
    assert r.json()["consent_required"] is True
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    s = client.get("/auth/consent-status", headers=auth)
    assert s.json()["consent_required"] is True
    assert s.json()["notice"]["version"] == 1
    assert s.json()["notice"]["body_markdown"]

    c = client.post("/auth/consent", headers=auth)
    assert c.status_code == 200
    assert c.json()["consent_required"] is False

    # idempotent
    c2 = client.post("/auth/consent", headers=auth)
    assert c2.status_code == 200

    s2 = client.get("/auth/consent-status", headers=auth)
    assert s2.json()["consent_required"] is False

    r2 = client.post("/auth/login", json={"email": worker.email, "password": _PASSWORD})
    assert r2.json()["consent_required"] is False


async def test_refresh_reports_consent_required_then_false_after_consent(
    client: TestClient, db_session: AsyncSession
) -> None:
    """A worker already logged in when a new notice is published learns of it on the next
    token refresh, not only on a fresh login."""
    worker = await _seed(db_session)
    login = client.post("/auth/login", json={"email": worker.email, "password": _PASSWORD})
    refresh_token = login.json()["refresh_token"]

    before = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post("/auth/consent", headers=auth)
    after = client.post("/auth/refresh", json={"refresh_token": refresh_token})

    assert before.status_code == 200, before.text
    assert before.json()["consent_required"] is True
    assert after.json()["consent_required"] is False


async def test_consent_status_requires_auth(client: TestClient) -> None:
    assert client.get("/auth/consent-status").status_code in (401, 403)
    assert client.post("/auth/consent").status_code in (401, 403)
