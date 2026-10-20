"""Etapa 11 (RNF10 / RNF12) — `GET /me/personal-data`: the caller's own LGPD aggregate,
and the audit-trail entry the access itself writes."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
)
from app.domain.execution.models import Execution, ExecutionSource
from app.domain.identity.models import User, UserRole
from app.domain.privacy.models import ConsentRecord, PrivacyNotice
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_route

_PASSWORD = "senha-forte-o-suficiente"


@dataclass
class SeededWorker:
    user: User
    worker: FieldWorker


async def _reseed_notice(db_session: AsyncSession) -> PrivacyNotice:
    await db_session.execute(text("DELETE FROM privacy_notices"))
    notice = PrivacyNotice(
        version=1,
        title="Aviso de Privacidade",
        body_markdown="# Aviso\nTratamos seus dados conforme a LGPD.",
        published_at=datetime.now(UTC),
        is_current=True,
    )
    db_session.add(notice)
    await db_session.flush()
    return notice


async def _seed_worker_with_execution(db_session: AsyncSession) -> SeededWorker:
    notice = await _reseed_notice(db_session)
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    user = User(
        name="Joana Field",
        email="joana@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add_all([company, user])
    await db_session.flush()
    worker = FieldWorker(
        full_name="Joana Field",
        contractor_company_id=company.id,
        user_id=user.id,
        status=FieldWorkerStatus.APPROVED,
    )
    db_session.add(worker)
    await db_session.flush()

    building = Building(name="Bloco CN", campus_area="Central")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Terreo")
    db_session.add(floor)
    await db_session.flush()
    point = ServicePoint(
        floor_id=floor.id,
        name="Sala 1",
        description="Sala de aula",
        latitude=-7.21,
        longitude=-35.90,
    )
    db_session.add(point)
    await db_session.flush()
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=[point])
    stop = route.stops[0]

    db_session.add(
        Execution(
            route_stop_id=stop.id,
            field_worker_id=worker.id,
            checked_in_at=datetime.now(UTC),
            synced_at=datetime.now(UTC),
            source=ExecutionSource.APP,
            idempotency_key=uuid.uuid4(),
        )
    )
    db_session.add(
        ConsentRecord(
            user_id=user.id,
            privacy_notice_id=notice.id,
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    return SeededWorker(user=user, worker=worker)


async def _seed_manager(db_session: AsyncSession) -> User:
    user = User(
        name="Larissa Almeida",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    db_session.add(user)
    await db_session.commit()
    return user


def _token(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


async def test_personal_data_reflects_own_records_and_writes_audit(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed_worker_with_execution(db_session)
    token = _token(client, seeded.user.email)

    response = client.get("/me/personal-data", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["email"] == seeded.user.email
    assert body["field_worker"]["id"] == str(seeded.worker.id)
    assert body["executions_summary"]["total"] == 1
    assert body["executions_summary"]["first_at"] is not None
    assert body["positions_summary"]["total"] == 0
    assert body["evidence_summary"] == {"total_items": 0, "total_bytes": 0}
    assert body["consent_history"][0]["notice_version"] == 1
    # The login just before this call is itself an auth event tied to the user.
    assert any(e["event"] == "LOGIN_SUCCESS" for e in body["auth_events"])

    rows = (
        (
            await db_session.execute(
                select(AuditTrail).where(
                    AuditTrail.entity_type == "personal_data",
                    AuditTrail.action == "SELF_ACCESS",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_id == seeded.user.id


async def test_personal_data_for_manager_without_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = await _seed_manager(db_session)
    token = _token(client, manager.email)

    response = client.get("/me/personal-data", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["field_worker"] is None
    assert body["executions_summary"] == {"total": 0, "first_at": None, "last_at": None}
    assert body["consent_history"] == []
    assert body["notifications_summary"] == {"total": 0, "unread": 0}


async def test_personal_data_requires_auth(client: TestClient) -> None:
    assert client.get("/me/personal-data").status_code in (401, 403)
