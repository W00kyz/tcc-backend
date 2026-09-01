import uuid
from datetime import UTC, datetime

import pytest
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop


@pytest.mark.asyncio
async def test_execution_defaults_review_none_and_empty_flags(db_session: AsyncSession) -> None:
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
    await db_session.commit()

    reloaded = await db_session.scalar(select(Execution).where(Execution.id == execution.id))
    assert reloaded is not None
    assert reloaded.review_status is ExecutionReviewStatus.NONE
    assert reloaded.validation_flags == []


@pytest.mark.asyncio
async def test_evidence_note_without_object_key_is_allowed_photo_without_is_not(
    db_session: AsyncSession,
) -> None:
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

    db_session.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.NOTE,
            text_body="tudo certo",
            captured_at=datetime.now(UTC),
        )
    )
    await db_session.commit()  # no raise

    db_session.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.PHOTO,
            captured_at=datetime.now(UTC),  # missing object_key/sha256/... -> CHECK fails
        )
    )
    with pytest.raises(IntegrityError, match="evidence_items_kind_shape"):
        await db_session.commit()
