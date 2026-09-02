"""RF33 — evidence attachment: a field worker uploads a photo or a note to an execution,
and managers (or the owning worker) list them and fetch photo bytes through this API
instead of a public MinIO URL.

Transaction discipline: `evidence_service` flushes and this router owns `db.commit()`
(the `execution/service.py` self-committing exception does not extend to evidence). No
audit trail — evidence is field-worker content, not a manager action.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._execution_common import current_worker
from app.api.deps import require_role
from app.core.object_store import ObjectStore
from app.db.session import get_db
from app.domain.execution.evidence_service import (
    EvidenceContentMissingError,
    EvidenceIntegrityError,
    EvidenceOwnershipError,
    add_note,
    add_photo,
    list_evidence,
    load_content,
)
from app.domain.execution.models import EvidenceItem, EvidenceKind, Execution
from app.domain.identity.models import User, UserRole

router = APIRouter(tags=["evidence"])

# Photos are phone snapshots re-encoded by the app before upload; 5 MiB is generous headroom
# and keeps a single evidence sync from ballooning on a weak connection (spec §8).
_MAX_PHOTO_BYTES = 5 * 1024 * 1024

_READ_ROLES = (UserRole.FIELD_WORKER, UserRole.MANAGER, UserRole.ADMIN)


class EvidenceItemOut(BaseModel):
    id: UUID
    kind: str
    text_body: str | None
    captured_at: datetime
    content_type: str | None
    byte_size: int | None
    created_at: datetime
    # The authenticated proxy path for a PHOTO; None for a NOTE. `object_key` is never exposed.
    content_url: str | None


class EvidenceNoteRequest(BaseModel):
    text_body: str
    captured_at: datetime


def _to_out(item: EvidenceItem) -> EvidenceItemOut:
    is_photo = item.kind is EvidenceKind.PHOTO
    return EvidenceItemOut(
        id=item.id,
        kind=item.kind.value,
        text_body=item.text_body,
        captured_at=item.captured_at,
        content_type=item.content_type,
        byte_size=item.byte_size,
        created_at=item.created_at,
        content_url=f"/evidence/{item.id}/content" if is_photo else None,
    )


async def _load_execution(db: AsyncSession, execution_id: UUID) -> Execution:
    execution = await db.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Execution "{execution_id}" not found.')
    return execution


async def _require_owning_worker(db: AsyncSession, user: User, execution: Execution) -> UUID:
    """The caller must be the field worker who owns `execution`; returns their worker id."""
    worker = await current_worker(db, user)
    if worker is None or worker.id != execution.field_worker_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the execution's own field worker may do this."
        )
    return worker.id


async def _authorize_read(db: AsyncSession, user: User, execution: Execution) -> None:
    """Managers and admins always; a field worker only for their own execution."""
    if user.role in (UserRole.MANAGER, UserRole.ADMIN):
        return
    await _require_owning_worker(db, user, execution)


@router.post(
    "/executions/{execution_id}/evidence/photo",
    response_model=EvidenceItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_photo(
    execution_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    sha256: Annotated[str, Form()],
    captured_at: Annotated[datetime, Form()],
) -> EvidenceItemOut:
    execution = await _load_execution(db, execution_id)
    worker_id = await _require_owning_worker(db, user, execution)

    content_type = file.content_type or "image/jpeg"
    if not content_type.startswith("image/"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f'Evidence photo content type "{content_type}" is not an "image/*" type.',
        )

    data = await file.read()
    if len(data) > _MAX_PHOTO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Evidence photo is {len(data)} bytes; the limit is {_MAX_PHOTO_BYTES} bytes.",
        )

    store: ObjectStore = request.app.state.object_store
    try:
        item = await add_photo(
            db,
            store,
            execution=execution,
            uploader_worker_id=worker_id,
            data=data,
            declared_sha256=sha256,
            content_type=content_type,
            captured_at=captured_at,
        )
    except EvidenceIntegrityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except EvidenceOwnershipError as exc:  # pragma: no cover - endpoint checks ownership first
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await db.commit()
    return _to_out(item)


@router.post(
    "/executions/{execution_id}/evidence/note",
    response_model=EvidenceItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_note(
    execution_id: UUID,
    body: EvidenceNoteRequest,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EvidenceItemOut:
    execution = await _load_execution(db, execution_id)
    worker_id = await _require_owning_worker(db, user, execution)

    if not body.text_body.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Evidence note text_body must not be empty."
        )

    item = await add_note(
        db,
        execution=execution,
        uploader_worker_id=worker_id,
        text_body=body.text_body,
        captured_at=body.captured_at,
    )
    await db.commit()
    return _to_out(item)


@router.get("/executions/{execution_id}/evidence", response_model=list[EvidenceItemOut])
async def list_execution_evidence(
    execution_id: UUID,
    user: Annotated[User, Depends(require_role(*_READ_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[EvidenceItemOut]:
    execution = await _load_execution(db, execution_id)
    await _authorize_read(db, user, execution)
    items = await list_evidence(db, execution_id=execution_id)
    return [_to_out(item) for item in items]


@router.get("/evidence/{evidence_id}/content")
async def get_evidence_content(
    evidence_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_role(*_READ_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    item = await db.get(EvidenceItem, evidence_id)
    if item is None or item.kind is not EvidenceKind.PHOTO:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Evidence "{evidence_id}" has no downloadable content.'
        )
    execution = await db.get(Execution, item.execution_id)
    assert execution is not None  # FK guarantees the parent row exists
    await _authorize_read(db, user, execution)

    store: ObjectStore = request.app.state.object_store
    try:
        data, content_type = await load_content(db, store, evidence_id=evidence_id)
    except EvidenceContentMissingError as exc:  # pragma: no cover - kind check above covers it
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return StreamingResponse(iter([data]), media_type=content_type)
