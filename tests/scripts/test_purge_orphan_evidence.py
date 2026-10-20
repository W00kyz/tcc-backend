"""The orphan-evidence purge run after the allocation reset: objects no evidence row points at
are deleted, objects still referenced are kept."""

import uuid
from datetime import UTC, datetime

from app.domain.execution.models import EvidenceItem, EvidenceKind, Execution, ExecutionSource
from app.scripts.purge_orphan_evidence import purge_orphan_evidence
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop
from tests.support.object_store import FakeObjectStore


async def _photo_row(db: AsyncSession, object_key: str) -> None:
    stop, worker = await seed_route_with_one_stop(db)
    now = datetime.now(UTC)
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=now,
        synced_at=now,
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db.add(execution)
    await db.flush()
    db.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.PHOTO,
            object_key=object_key,
            content_type="image/jpeg",
            byte_size=3,
            sha256="0" * 64,
            captured_at=now,
        )
    )
    await db.commit()


async def test_purge_deletes_only_unreferenced_objects(db_session: AsyncSession) -> None:
    await _photo_row(db_session, "evidence/kept.jpg")
    store = FakeObjectStore()
    for key in ("evidence/kept.jpg", "evidence/wiped-1.jpg", "evidence/wiped-2.jpg"):
        await store.put(key, b"jpg", content_type="image/jpeg")

    deleted = await purge_orphan_evidence(db_session, store)

    assert sorted(deleted) == ["evidence/wiped-1.jpg", "evidence/wiped-2.jpg"]
    assert await store.list_keys() == ["evidence/kept.jpg"]


async def test_purge_after_the_reset_empties_the_bucket(db_session: AsyncSession) -> None:
    store = FakeObjectStore()
    await store.put("evidence/a.jpg", b"jpg", content_type="image/jpeg")

    assert await purge_orphan_evidence(db_session, store) == ["evidence/a.jpg"]
    assert await store.list_keys() == []
