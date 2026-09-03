import uuid
from datetime import UTC, datetime

import pytest
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionSource,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop


async def _seed_execution(db_session: AsyncSession) -> Execution:
    stop, worker = await seed_route_with_one_stop(db_session)
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=datetime.now(UTC),
        synced_at=datetime.now(UTC),
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db_session.add(execution)
    await db_session.flush()
    return execution


@pytest.mark.asyncio
async def test_execution_stores_clock_skew_seconds(db_session: AsyncSession) -> None:
    execution = await _seed_execution(db_session)
    execution.clock_skew_seconds = 420.0
    await db_session.commit()

    reloaded = await db_session.scalar(select(Execution).where(Execution.id == execution.id))
    assert reloaded is not None
    assert reloaded.clock_skew_seconds == 420.0


@pytest.mark.asyncio
async def test_evidence_idempotency_key_is_unique(db_session: AsyncSession) -> None:
    execution = await _seed_execution(db_session)
    shared_key = uuid.uuid4()
    db_session.add_all(
        [
            EvidenceItem(
                execution_id=execution.id,
                kind=EvidenceKind.NOTE,
                text_body="primeira",
                captured_at=datetime.now(UTC),
                idempotency_key=shared_key,
            ),
            EvidenceItem(
                execution_id=execution.id,
                kind=EvidenceKind.NOTE,
                text_body="segunda",
                captured_at=datetime.now(UTC),
                idempotency_key=shared_key,
            ),
        ]
    )
    with pytest.raises(IntegrityError, match="ix_evidence_items_idempotency_key"):
        await db_session.commit()
