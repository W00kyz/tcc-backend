"""RF33 — photo/note evidence attached to an execution (spec §4.1).

This module flushes, never commits: the `execution/service.py` "service owns its
transaction" exception is documented and does not extend here, so the evidence endpoint
owns `db.commit()`. No `record_audit_trail` either — evidence is field-worker content,
not a manager action, same as check-in.
"""

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.object_store import ObjectStore
from app.domain.execution.models import EvidenceItem, EvidenceKind, Execution

# The upload endpoint enforces the byte ceiling and the JPEG magic-byte check (so it can
# answer 413/422 cleanly); this module only guards ownership and integrity.
_JPEG_SUFFIX = ".jpg"
# Evidence photos are always JPEG (endpoint-validated). The content type is server-chosen
# and stored as this literal — never the client's `Content-Type` header — so the content
# proxy can serve it with a fixed, safe media type (no SVG/script smuggling).
_EVIDENCE_CONTENT_TYPE = "image/jpeg"


class EvidenceOwnershipError(Exception):
    """The uploading field worker does not own the target execution."""


class EvidenceIntegrityError(Exception):
    """The declared sha256 does not match the received bytes — nothing was written."""


class EvidenceContentMissingError(Exception):
    """The evidence row is absent or carries no object (a NOTE) — no content to serve."""


async def add_photo(
    db: AsyncSession,
    store: ObjectStore,
    *,
    execution: Execution,
    uploader_worker_id: UUID,
    data: bytes,
    declared_sha256: str,
    captured_at: datetime,
    idempotency_key: UUID | None = None,
) -> tuple[EvidenceItem, bool]:
    """Verify integrity, put the object in MinIO, then flush the row. The hash is checked
    BEFORE `store.put` so a mismatch leaves neither an object nor a row. The stored
    content type is always `image/jpeg` (the endpoint enforces JPEG), never the client's
    header. Returns `(item, created)`; a repeat call with a seen `idempotency_key`
    returns the stored row with `created=False` and touches neither MinIO nor the row."""
    _guard_ownership(execution, uploader_worker_id)

    existing = await _find_by_key(db, idempotency_key)
    if existing is not None:
        return existing, False

    digest = hashlib.sha256(data).hexdigest()
    # Accept an uppercase-hex declaration too (`hexdigest()` is lowercase).
    if digest != declared_sha256.lower():
        raise EvidenceIntegrityError(
            f'Declared sha256 "{declared_sha256}" does not match the {len(data)} received '
            f'bytes (actual "{digest}"); nothing was stored.'
        )

    object_key = f"evidence/{execution.id}/{uuid4()}{_JPEG_SUFFIX}"
    await store.put(object_key, data, content_type=_EVIDENCE_CONTENT_TYPE)

    item = EvidenceItem(
        execution_id=execution.id,
        kind=EvidenceKind.PHOTO,
        object_key=object_key,
        content_type=_EVIDENCE_CONTENT_TYPE,
        byte_size=len(data),
        sha256=digest,
        captured_at=captured_at,
        idempotency_key=idempotency_key,
    )
    db.add(item)
    await db.flush()
    return item, True


async def add_note(
    db: AsyncSession,
    *,
    execution: Execution,
    uploader_worker_id: UUID,
    text_body: str,
    captured_at: datetime,
    idempotency_key: UUID | None = None,
) -> tuple[EvidenceItem, bool]:
    """Attach a free-text note (no object). The endpoint rejects empty text with 422.
    Returns `(item, created)`; a repeat call with a seen `idempotency_key` returns the
    stored row with `created=False`."""
    _guard_ownership(execution, uploader_worker_id)

    existing = await _find_by_key(db, idempotency_key)
    if existing is not None:
        return existing, False

    item = EvidenceItem(
        execution_id=execution.id,
        kind=EvidenceKind.NOTE,
        text_body=text_body,
        captured_at=captured_at,
        idempotency_key=idempotency_key,
    )
    db.add(item)
    await db.flush()
    return item, True


async def _find_by_key(db: AsyncSession, idempotency_key: UUID | None) -> EvidenceItem | None:
    """The evidence row already stored under this sync key, if the app is retrying an
    upload it already completed (spec §8). `None` key means the client sent no key."""
    if idempotency_key is None:
        return None
    row: EvidenceItem | None = await db.scalar(
        select(EvidenceItem).where(EvidenceItem.idempotency_key == idempotency_key)
    )
    return row


async def list_evidence(db: AsyncSession, *, execution_id: UUID) -> list[EvidenceItem]:
    """Every evidence item on the execution, oldest first."""
    rows = await db.scalars(
        select(EvidenceItem)
        .where(EvidenceItem.execution_id == execution_id)
        .order_by(EvidenceItem.created_at)
    )
    return list(rows)


async def load_content(
    db: AsyncSession, store: ObjectStore, *, evidence_id: UUID
) -> tuple[bytes, str]:
    """The object bytes and content type for a PHOTO. Raises `EvidenceContentMissingError`
    when the row is absent or is a NOTE — the endpoint maps that to 404."""
    item = await db.get(EvidenceItem, evidence_id)
    if item is None or item.object_key is None:
        raise EvidenceContentMissingError(f'Evidence "{evidence_id}" has no stored object.')
    return await store.get(item.object_key)


def _guard_ownership(execution: Execution, uploader_worker_id: UUID) -> None:
    if execution.field_worker_id != uploader_worker_id:
        raise EvidenceOwnershipError(
            f'Field worker "{uploader_worker_id}" does not own execution "{execution.id}" '
            f'(owned by "{execution.field_worker_id}").'
        )
