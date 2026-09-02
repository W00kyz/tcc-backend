"""RF32 — `GET/PUT /settings`, the configurable check radius, plus the defensive
`get_settings_row` singleton recreation (pre-flight Ruling P3)."""

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.identity.models import User, UserRole
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row, update_settings
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
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


async def test_get_settings_returns_default_50(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    token = _login(client, "gerente@empresa.com")

    response = client.get("/settings", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json() == {"check_radius_meters": 50, "updated_at": None}


async def test_put_settings_admin_updates_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    response = client.put("/settings", json={"check_radius_meters": 120}, headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["check_radius_meters"] == 120
    assert response.json()["updated_at"] is not None

    getr = client.get("/settings", headers=_auth(token))
    assert getr.json()["check_radius_meters"] == 120

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(
            AuditTrail.entity_type == "system_settings",
            AuditTrail.action == "update_settings",
        )
    )
    assert audit_count == 1


async def test_put_settings_out_of_range_422(client: TestClient, db_session: AsyncSession) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    assert (
        client.put("/settings", json={"check_radius_meters": 0}, headers=_auth(token)).status_code
        == 422
    )
    assert (
        client.put(
            "/settings", json={"check_radius_meters": 1001}, headers=_auth(token)
        ).status_code
        == 422
    )


async def test_put_settings_manager_forbidden_403(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    token = _login(client, "gerente@empresa.com")

    response = client.put("/settings", json={"check_radius_meters": 80}, headers=_auth(token))

    assert response.status_code == 403


async def test_get_settings_row_creates_singleton_when_missing(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(SystemSettings))
    await db_session.commit()
    assert await db_session.scalar(select(SystemSettings)) is None

    row = await get_settings_row(db_session)
    await db_session.commit()

    assert row.check_radius_meters == 50
    assert await db_session.scalar(select(func.count()).select_from(SystemSettings)) == 1


async def test_update_settings_stamps_actor_and_timestamp(db_session: AsyncSession) -> None:
    actor = User(
        name="admin",
        email="admin@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.ADMIN,
    )
    db_session.add(actor)
    await db_session.flush()

    row = await update_settings(db_session, actor_id=actor.id, check_radius_meters=250)
    await db_session.commit()

    assert row.check_radius_meters == 250
    assert row.updated_by == actor.id
    assert row.updated_at is not None
