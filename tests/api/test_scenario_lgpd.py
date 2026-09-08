"""Cenário LGPD ponta a ponta (spec §2 Ruling 11, RNF10 / RNF12).

O entregável da Etapa 11: um titular de dados passa por todo o ciclo LGPD contra a stack
real (Postgres/PostGIS testcontainer, HTTP real) —

1. login devolve ``consent_required = true`` enquanto não há aceite do aviso corrente;
2. ``POST /auth/consent`` registra o aceite e o próximo login devolve ``false``;
3. o titular executa uma rota (um check-in + posições de GPS reportadas);
4. ``GET /me/personal-data`` reflete as contagens e grava um ``audit_trail SELF_ACCESS``;
5. um gestor lê ``GET /executions?field_worker_id=`` e isso grava um ``PERSONAL_DATA_ACCESS``
   com ``entity_id`` = ``user_id`` do titular;
6. ``run_retention_purge`` com retenção de 1 dia e um ``now`` no futuro distante apaga as
   posições e grava um ``audit_trail PURGE``;
7. ``GET /me/personal-data`` de novo mostra ``positions_summary.total == 0`` — e as execuções
   permanecem (o expurgo toca só posições e evidências, nunca o histórico de execução).

Marcado ``slow`` e fora do run padrão (``-m "not slow"`` no ``addopts``); selecione com
``uv run pytest -m slow``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings
from app.domain.audit.models import AuditTrail
from app.domain.execution.models import EvidenceItem
from app.domain.identity.models import UserRole
from app.domain.monitoring.models import WorkerPosition
from app.domain.privacy.models import PrivacyNotice
from app.domain.retention.purge import run_retention_purge
from app.domain.settings.service import get_settings_row
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import _WORKER_EMAIL, _add_user, _login, _post, _seed_route

_MANAGER_EMAIL = "gestora@empresa.com"
_PASSWORD = "senha-forte-o-suficiente"


class _RecordingObjectStore:
    """Named fake — records deletes, never touches the network (spec §8)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:  # pragma: no cover
        raise AssertionError("retention purge must not put objects")

    async def get(self, key: str) -> tuple[bytes, str]:  # pragma: no cover
        raise AssertionError("retention purge must not read objects")

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _reseed_current_notice(db_session: AsyncSession) -> None:
    """Truncation between tests may have dropped the migration-seeded v1 notice."""
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
    await db_session.commit()


async def _count(db_session: AsyncSession, action: str, entity_type: str) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditTrail)
            .where(AuditTrail.action == action, AuditTrail.entity_type == entity_type)
        )
        or 0
    )


@pytest.mark.slow
async def test_lgpd_consent_personal_data_and_retention_end_to_end(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _reseed_current_notice(db_session)
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker = seeded.worker

    # 1. Login before consent -> consent_required.
    first = client.post("/auth/login", json={"email": _WORKER_EMAIL, "password": _PASSWORD})
    assert first.status_code == 200, first.text
    assert first.json()["consent_required"] is True
    worker_token = first.json()["access_token"]
    auth_headers = {"Authorization": f"Bearer {worker_token}"}

    # 2. Accept the notice; the next login reports no consent required.
    consent = client.post("/auth/consent", headers=auth_headers)
    assert consent.status_code == 200, consent.text
    assert consent.json()["consent_required"] is False

    second = client.post("/auth/login", json={"email": _WORKER_EMAIL, "password": _PASSWORD})
    assert second.json()["consent_required"] is False
    worker_token = second.json()["access_token"]
    auth_headers = {"Authorization": f"Bearer {worker_token}"}

    # 3. Execute the route: one check-in + a few reported GPS fixes.
    check_in = _post(client, worker_token, seeded.qr_code.public_code)
    assert check_in.status_code == 201, check_in.text

    for minutes_ago in (6, 4, 2):
        reported_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        position = client.post(
            "/worker-positions",
            json={
                "route_id": str(seeded.route.id),
                "latitude": -7.2001,
                "longitude": -35.9001,
                "reported_at": reported_at.isoformat(),
                "accuracy_m": 12.0,
            },
            headers=auth_headers,
        )
        assert position.status_code == 201, position.text

    # 4. GET /me/personal-data reflects the counts and writes exactly one SELF_ACCESS row.
    personal = client.get("/me/personal-data", headers=auth_headers)
    assert personal.status_code == 200, personal.text
    body = personal.json()
    assert body["executions_summary"]["total"] >= 1
    assert body["positions_summary"]["total"] >= 1
    assert await _count(db_session, "SELF_ACCESS", "personal_data") == 1

    # 5. Manager reads the worker's execution history -> one PERSONAL_DATA_ACCESS row.
    manager_token = _login(client, email=_MANAGER_EMAIL)
    history = client.get(
        f"/executions?field_worker_id={worker.id}",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert history.status_code == 200, history.text

    access_rows = (
        (
            await db_session.execute(
                select(AuditTrail).where(AuditTrail.action == "PERSONAL_DATA_ACCESS")
            )
        )
        .scalars()
        .all()
    )
    assert len(access_rows) == 1
    assert access_rows[0].after_data == {"context": "execution_history"}
    assert access_rows[0].entity_id == worker.user_id

    # 6. Retention set to 1 day; purge with `now` far in the future deletes the positions.
    settings_row = await get_settings_row(db_session)
    settings_row.worker_position_retention_days = 1
    settings_row.evidence_retention_days = 1
    await db_session.commit()

    store = _RecordingObjectStore()
    result = await run_retention_purge(
        db_session, object_store=store, now=datetime.now(UTC) + timedelta(days=400)
    )
    assert result.positions_deleted >= 1
    assert await _count(db_session, "PURGE", "retention") == 1

    surviving_positions = await db_session.scalar(select(func.count()).select_from(WorkerPosition))
    assert surviving_positions == 0
    surviving_evidence = await db_session.scalar(select(func.count()).select_from(EvidenceItem))
    assert surviving_evidence == 0

    # 7. GET /me/personal-data again: positions gone, executions untouched (the boundary).
    after_purge = client.get("/me/personal-data", headers=auth_headers)
    assert after_purge.status_code == 200, after_purge.text
    after_body = after_purge.json()
    assert after_body["positions_summary"]["total"] == 0
    assert after_body["evidence_summary"] == {"total_items": 0, "total_bytes": 0}
    assert after_body["executions_summary"]["total"] == body["executions_summary"]["total"]
    assert await _count(db_session, "SELF_ACCESS", "personal_data") == 2
