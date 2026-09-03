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

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
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

# The first bytes of every JPEG (SOI marker + start of the first segment). We accept JPEG
# only: an `image/svg+xml` body with a `<script>` would otherwise be served inline from this
# API's own origin by the content proxy — stored XSS. `file.content_type` is ignored entirely.
_JPEG_MAGIC = b"\xff\xd8\xff"

# What the content proxy serves a stored photo as — a fixed, non-sniffable media type.
_EVIDENCE_MEDIA_TYPE = "image/jpeg"

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
    # spec §3.4: a short field note, not a report. Whitespace-only is rejected in the handler.
    text_body: str = Field(min_length=1, max_length=2000)
    captured_at: datetime
    # spec §8: the app's sync worker sends the same key when it retries after a network
    # drop; a repeat POST returns the stored item (200) instead of a second row.
    idempotency_key: UUID


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
    response: Response,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    sha256: Annotated[str, Form()],
    captured_at: Annotated[datetime, Form()],
    idempotency_key: Annotated[UUID, Form()],
) -> EvidenceItemOut:
    execution = await _load_execution(db, execution_id)
    worker_id = await _require_owning_worker(db, user, execution)

    # Read one byte past the ceiling — enough to know it is over the limit without
    # buffering an arbitrarily large upload into memory.
    data = await file.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Evidence photo exceeds the {_MAX_PHOTO_BYTES}-byte limit.",
        )
    if data[:3] != _JPEG_MAGIC:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only JPEG images are accepted.")

    store: ObjectStore = request.app.state.object_store
    try:
        item, created = await add_photo(
            db,
            store,
            execution=execution,
            uploader_worker_id=worker_id,
            data=data,
            declared_sha256=sha256,
            captured_at=captured_at,
            idempotency_key=idempotency_key,
        )
    except EvidenceIntegrityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except EvidenceOwnershipError as exc:  # pragma: no cover - endpoint checks ownership first
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_out(item)


@router.post(
    "/executions/{execution_id}/evidence/note",
    response_model=EvidenceItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_note(
    execution_id: UUID,
    body: EvidenceNoteRequest,
    response: Response,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EvidenceItemOut:
    execution = await _load_execution(db, execution_id)
    worker_id = await _require_owning_worker(db, user, execution)

    if not body.text_body.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Evidence note text_body must not be empty."
        )

    item, created = await add_note(
        db,
        execution=execution,
        uploader_worker_id=worker_id,
        text_body=body.text_body,
        captured_at=body.captured_at,
        idempotency_key=body.idempotency_key,
    )
    await db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
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
        data, _stored_type = await load_content(db, store, evidence_id=evidence_id)
    except EvidenceContentMissingError as exc:  # pragma: no cover - kind check above covers it
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # Fixed media type + nosniff: the stored bytes are JPEG-validated on upload, and the
    # browser must not be free to re-interpret them as anything executable.
    return StreamingResponse(
        iter([data]),
        media_type=_EVIDENCE_MEDIA_TYPE,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="evidence.jpg"',
        },
    )
