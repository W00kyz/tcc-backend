"""Etapa 11 (RNF12) — a manager/admin reading another person's personal data writes one
`PERSONAL_DATA_ACCESS` audit row; the owning worker's self-access and blanket list
endpoints write none."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.execution.models import Execution, ExecutionSource
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_route

_PASSWORD = "senha-forte-o-suficiente"


@dataclass
class SeededExecution:
    worker_user: User
    field_worker: FieldWorker
    execution: Execution


async def _audit_rows(db_session: AsyncSession, *, context: str | None = None) -> list[AuditTrail]:
    rows = (
        (
            await db_session.execute(
                select(AuditTrail).where(AuditTrail.action == "PERSONAL_DATA_ACCESS")
            )
        )
        .scalars()
        .all()
    )
    if context is not None:
        rows = [r for r in rows if (r.after_data or {}).get("context") == context]
    return list(rows)


async def _seed_worker_execution(
    db_session: AsyncSession, email: str = "worker-a@pu.ufcg.edu.br"
) -> SeededExecution:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    user = User(
        name="Worker A",
        email=email,
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add_all([company, user])
    await db_session.flush()
    worker = FieldWorker(full_name="Worker A", contractor_company_id=company.id, user_id=user.id)
    db_session.add(worker)
    await db_session.flush()

    building = Building(name="Bloco CN", campus_area="Central")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Terreo")
    db_session.add(floor)
    await db_session.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Sala 1", description="Sala", latitude=-7.21, longitude=-35.90
    )
    db_session.add(point)
    await db_session.flush()
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=[point])
    stop = route.stops[0]
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=datetime.now(UTC),
        synced_at=datetime.now(UTC),
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db_session.add(execution)
    await db_session.commit()
    return SeededExecution(worker_user=user, field_worker=worker, execution=execution)


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


async def test_execution_history_with_field_worker_filter_is_audited(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed_worker_execution(db_session)
    await _seed_manager(db_session)
    token = _token(client, "larissa@pu.ufcg.edu.br")

    response = client.get(
        f"/executions?field_worker_id={seeded.field_worker.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, context="execution_history")
    assert len(rows) == 1
    assert rows[0].entity_id == seeded.worker_user.id
    assert rows[0].after_data == {"context": "execution_history"}


async def test_execution_history_without_filter_is_not_audited(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_worker_execution(db_session)
    await _seed_manager(db_session)
    token = _token(client, "larissa@pu.ufcg.edu.br")

    response = client.get("/executions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert await _audit_rows(db_session) == []


async def test_manager_viewing_worker_evidence_is_audited(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed_worker_execution(db_session)
    await _seed_manager(db_session)
    token = _token(client, "larissa@pu.ufcg.edu.br")

    response = client.get(
        f"/executions/{seeded.execution.id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, context="evidence_view")
    assert len(rows) == 1
    assert rows[0].entity_id == seeded.worker_user.id


async def test_owning_worker_viewing_own_evidence_is_not_audited(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed_worker_execution(db_session)
    token = _token(client, seeded.worker_user.email)

    response = client.get(
        f"/executions/{seeded.execution.id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert await _audit_rows(db_session) == []


async def test_route_list_is_not_blanket_audited(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed_worker_execution(db_session)
    await _seed_manager(db_session)
    token = _token(client, "larissa@pu.ufcg.edu.br")

    response = client.get("/routes", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert await _audit_rows(db_session) == []
