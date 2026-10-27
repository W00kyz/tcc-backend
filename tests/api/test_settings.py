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
    assert response.json()["check_radius_meters"] == 50
    assert response.json()["updated_at"] is None


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


async def test_get_settings_includes_etapa_9_defaults(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    token = _login(client, "gerente@empresa.com")

    body = client.get("/settings", headers=_auth(token)).json()

    assert body["alert_scan_interval_seconds"] == 60
    assert body["no_evidence_grace_min"] == 30
    assert body["position_report_interval_seconds"] == 60
    assert not {"late_start_tolerance_min", "absence_tolerance_min", "late_stop_tolerance_min"} & (
        set(body)
    )


async def test_get_settings_includes_allocation_defaults(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    token = _login(client, "gerente@empresa.com")

    body = client.get("/settings", headers=_auth(token)).json()

    assert body["allocation_batch_size"] == 4
    assert (body["campus_min_lat"], body["campus_max_lat"]) == (-7.2250, -7.2050)
    assert (body["campus_min_lng"], body["campus_max_lng"]) == (-35.9200, -35.8980)
    assert body["outside_campus_release_minutes"] == 10
    assert body["location_silence_warning_minutes"] == 30
    assert body["location_silence_release_minutes"] == 45
    assert body["workday_end_time"] == "17:00:00"
    assert body["service_weekdays"] == [0, 1, 2, 3, 4]
    assert body["appeal_window_hours"] == 48


async def test_put_settings_updates_allocation_fields_and_audits_them_as_json(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    response = client.put(
        "/settings",
        json={
            "allocation_batch_size": 6,
            "workday_end_time": "16:30",
            "service_weekdays": [5, 0, 0, 2],
            "campus_min_lat": -7.23,
        },
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allocation_batch_size"] == 6
    assert body["workday_end_time"] == "16:30:00"
    assert body["service_weekdays"] == [0, 2, 5]  # sorted, duplicates dropped
    assert body["campus_min_lat"] == -7.23
    assert body["appeal_window_hours"] == 48  # untouched
    audit = await db_session.scalar(
        select(AuditTrail).where(AuditTrail.action == "update_settings")
    )
    assert audit is not None
    assert audit.after_data is not None
    assert audit.after_data["workday_end_time"] == "16:30:00"
    assert audit.before_data is not None
    assert audit.before_data["service_weekdays"] == [0, 1, 2, 3, 4]


async def test_put_settings_updates_etapa_9_fields_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    response = client.put(
        "/settings",
        json={"no_evidence_grace_min": 25, "position_report_interval_seconds": 30},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["no_evidence_grace_min"] == 25
    assert response.json()["position_report_interval_seconds"] == 30
    # Untouched fields keep their defaults (partial patch).
    assert response.json()["check_radius_meters"] == 50
    assert response.json()["alert_scan_interval_seconds"] == 60

    getr = client.get("/settings", headers=_auth(token)).json()
    assert getr["no_evidence_grace_min"] == 25

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(
            AuditTrail.entity_type == "system_settings",
            AuditTrail.action == "update_settings",
        )
    )
    assert audit_count == 1


async def test_put_settings_etapa_9_bounds_rejected_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    assert (
        client.put(
            "/settings", json={"alert_scan_interval_seconds": 0}, headers=_auth(token)
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/settings", json={"no_evidence_grace_min": -1}, headers=_auth(token)
        ).status_code
        == 422
    )


async def test_put_settings_allocation_bounds_rejected_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _add_user(db_session, "admin@empresa.com", UserRole.ADMIN)
    token = _login(client, "admin@empresa.com")

    bad_bodies: list[dict[str, object]] = [
        {"allocation_batch_size": 0},
        {"campus_max_lat": 91},
        {"campus_min_lng": -181},
        {"location_silence_release_minutes": 0},
        {"service_weekdays": []},
        {"service_weekdays": [7]},
        {"appeal_window_hours": 0},
        {"workday_end_time": "25:00"},
    ]
    for body in bad_bodies:
        assert client.put("/settings", json=body, headers=_auth(token)).status_code == 422, body


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

    row = await update_settings(db_session, actor_id=actor.id, changes={"check_radius_meters": 250})
    await db_session.commit()

    assert row.check_radius_meters == 250
    assert row.updated_by == actor.id
    assert row.updated_at is not None


async def test_update_settings_writes_only_the_passed_fields(
    db_session: AsyncSession,
) -> None:
    actor = User(
        name="admin",
        email="admin@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.ADMIN,
    )
    db_session.add(actor)
    await db_session.flush()

    row = await update_settings(
        db_session, actor_id=actor.id, changes={"no_evidence_grace_min": 20}
    )
    await db_session.commit()

    assert row.no_evidence_grace_min == 20
    # Everything else keeps its default.
    assert row.check_radius_meters == 50
    assert row.alert_scan_interval_seconds == 60
    assert row.position_report_interval_seconds == 60
    assert row.allocation_batch_size == 4
    assert row.service_weekdays == [0, 1, 2, 3, 4]
