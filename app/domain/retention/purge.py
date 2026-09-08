"""RNF10 — daily retention purge for personal data.

`run_retention_purge` deletes `worker_positions` and `evidence_items` older than the windows
configured on `system_settings` (`worker_position_retention_days` / `evidence_retention_days`;
0 = keep forever), removing each PHOTO's object from the store before its row, and writes one
`audit_trail` PURGE entry with the counts. The caller (a scheduler wrapper) owns the session;
this function owns the single `commit`.

    result = await run_retention_purge(db, object_store=store, now=datetime.now(UTC))
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.object_store import ObjectStore
from app.domain.audit.service import record_audit_trail
from app.domain.execution.models import EvidenceItem, EvidenceKind
from app.domain.monitoring.models import WorkerPosition
from app.domain.settings.service import get_settings_row

# `system_settings` is a singleton keyed by a boolean PK, so it has no UUID of its own; the
# audit trail's `entity_id` column is a non-nullable UUID, so a retention purge is recorded
# against the nil UUID (there is only ever one such stream).
_RETENTION_ENTITY_ID = uuid.UUID(int=0)


@dataclass(frozen=True)
class RetentionPurgeResult:
    positions_deleted: int
    evidence_deleted: int
    objects_deleted: int


async def run_retention_purge(
    db: AsyncSession, *, object_store: ObjectStore, now: datetime
) -> RetentionPurgeResult:
    settings_row = await get_settings_row(db)

    positions_deleted = await _purge_worker_positions(
        db, retention_days=settings_row.worker_position_retention_days, now=now
    )
    evidence_deleted, objects_deleted = await _purge_evidence(
        db, object_store, retention_days=settings_row.evidence_retention_days, now=now
    )

    result = RetentionPurgeResult(
        positions_deleted=positions_deleted,
        evidence_deleted=evidence_deleted,
        objects_deleted=objects_deleted,
    )
    await record_audit_trail(
        db,
        actor_id=None,
        entity_type="retention",
        entity_id=_RETENTION_ENTITY_ID,
        action="PURGE",
        before=None,
        after=asdict(result),
    )
    await db.commit()
    return result


async def _purge_worker_positions(db: AsyncSession, *, retention_days: int, now: datetime) -> int:
    if retention_days == 0:  # 0 = keep forever
        return 0
    cutoff = now - timedelta(days=retention_days)
    result = await db.execute(delete(WorkerPosition).where(WorkerPosition.received_at < cutoff))
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def _purge_evidence(
    db: AsyncSession, object_store: ObjectStore, *, retention_days: int, now: datetime
) -> tuple[int, int]:
    if retention_days == 0:  # 0 = keep forever
        return 0, 0
    cutoff = now - timedelta(days=retention_days)
    doomed = list(await db.scalars(select(EvidenceItem).where(EvidenceItem.captured_at < cutoff)))
    if not doomed:
        return 0, 0

    objects_deleted = 0
    for item in doomed:
        # Only PHOTO evidence has a stored object; delete it before the row so a failure here
        # leaves the row (and a chance to retry) rather than orphaning the object.
        if item.kind is EvidenceKind.PHOTO and item.object_key is not None:
            await object_store.delete(item.object_key)
            objects_deleted += 1

    ids = [item.id for item in doomed]
    await db.execute(delete(EvidenceItem).where(EvidenceItem.id.in_(ids)))
    return len(ids), objects_deleted
