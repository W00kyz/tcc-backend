"""RNF10 — `run_retention_purge` deletes personal data past its retention window, removes the
PHOTO objects first, and records one audit entry. 0 days means keep forever."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.audit.models import AuditTrail
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionSource,
)
from app.domain.monitoring.models import WorkerPosition
from app.domain.retention.purge import run_retention_purge
from app.domain.settings.service import get_settings_row
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class RecordingObjectStore:
    """Named fake — records deletes, never touches the network (spec §8)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:  # pragma: no cover
        raise AssertionError("retention purge must not put objects")

    async def get(self, key: str) -> tuple[bytes, str]:  # pragma: no cover
        raise AssertionError("retention purge must not read objects")

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed(db: AsyncSession) -> tuple[str, str]:
    """Seed 2 old + 1 fresh worker positions, 1 old PHOTO evidence + 1 fresh NOTE evidence.

    Returns (old_object_key, fresh_object_key-unused) — really just the doomed object key."""
    stop, worker = await seed_route_with_one_stop(db)
    old = _NOW - timedelta(days=40)
    fresh = _NOW - timedelta(days=10)

    for received_at in (old, old, fresh):
        db.add(
            WorkerPosition(
                field_worker_id=worker.id,
                route_id=stop.route_id,
                latitude=-7.2,
                longitude=-35.9,
                reported_at=received_at,
                received_at=received_at,
            )
        )

    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=_NOW - timedelta(days=45),
        synced_at=_NOW - timedelta(days=45),
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db.add(execution)
    await db.flush()

    old_key = "evidence/old-photo.jpg"
    db.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.PHOTO,
            object_key=old_key,
            content_type="image/jpeg",
            byte_size=1234,
            sha256="0" * 64,
            captured_at=old,
        )
    )
    db.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.NOTE,
            text_body="tudo certo",
            captured_at=fresh,
        )
    )
    await db.commit()
    return old_key, ""


async def _set_retention(db: AsyncSession, *, days: int) -> None:
    row = await get_settings_row(db)
    row.worker_position_retention_days = days
    row.evidence_retention_days = days
    await db.commit()


@pytest.mark.asyncio
async def test_purge_deletes_data_past_retention_and_audits(db_session: AsyncSession) -> None:
    old_key, _ = await _seed(db_session)
    await _set_retention(db_session, days=30)
    store = RecordingObjectStore()

    result = await run_retention_purge(db_session, object_store=store, now=_NOW)

    assert result.positions_deleted == 2
    assert result.evidence_deleted == 1
    assert result.objects_deleted == 1
    assert store.deleted == [old_key]

    surviving_positions = await db_session.scalar(select(func.count()).select_from(WorkerPosition))
    assert surviving_positions == 1
    surviving_evidence = await db_session.scalar(select(func.count()).select_from(EvidenceItem))
    assert surviving_evidence == 1

    audit = await db_session.scalar(select(AuditTrail).where(AuditTrail.entity_type == "retention"))
    assert audit is not None
    assert audit.action == "PURGE"
    assert audit.actor_id is None
    assert audit.after_data == {
        "positions_deleted": 2,
        "evidence_deleted": 1,
        "objects_deleted": 1,
    }


@pytest.mark.asyncio
async def test_purge_keeps_everything_when_retention_is_zero(db_session: AsyncSession) -> None:
    await _seed(db_session)
    await _set_retention(db_session, days=0)
    store = RecordingObjectStore()

    result = await run_retention_purge(db_session, object_store=store, now=_NOW)

    assert result == type(result)(0, 0, 0)
    assert store.deleted == []
    positions = await db_session.scalar(select(func.count()).select_from(WorkerPosition))
    assert positions == 3
    evidence = await db_session.scalar(select(func.count()).select_from(EvidenceItem))
    assert evidence == 2
