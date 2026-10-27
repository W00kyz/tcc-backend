"""RF43 / RF44 — the manager execution-history surface: a filterable paginated list, a detail
view bundling the scans / evidence / form answers, and review resolution.

Transaction discipline: `history` flushes and this router owns `db.commit()`. The list and
detail endpoints are pure reads; only `resolve_review` writes (the status change plus one
`audit_trail` row).
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail, record_personal_data_access
from app.domain.catalog.models import FieldWorker
from app.domain.execution.history import (
    ExecutionDetailRow,
    ExecutionFilters,
    ExecutionListRow,
    ExecutionNotFound,
    ReviewNotPending,
    get_execution_detail,
    list_executions,
    resolve_review,
)
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/executions", tags=["execution"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

_MAX_PAGE_SIZE = 200


class ExecutionListItem(BaseModel):
    execution_id: UUID
    field_worker_id: UUID
    field_worker_name: str
    service_point_id: UUID
    service_point_name: str
    building_id: UUID
    building_name: str
    floor_label: str
    checked_in_at: datetime
    checked_out_at: datetime | None
    geo_validation: str | None
    review_status: str
    validation_flags: list[str]
    source: str
    evidence_count: int
    route_id: UUID
    clock_skew_seconds: float | None  # app-reported device/server clock offset when flagged
    appeal_id: UUID | None  # set on a `source=APPEAL` execution: the accepted appeal


class PagedExecutions(BaseModel):
    items: list[ExecutionListItem]
    total: int
    page: int
    page_size: int


class ExecutionScan(BaseModel):
    kind: str
    geo_validation: str
    latitude: float | None
    longitude: float | None
    distance_m: float | None
    service_point_id: UUID | None
    scanned_at: datetime


class ExecutionEvidence(BaseModel):
    id: UUID
    kind: str
    text_body: str | None
    content_type: str | None
    byte_size: int | None
    captured_at: datetime
    created_at: datetime
    content_url: str | None  # the authenticated proxy path for a PHOTO; None for a NOTE
    # The app-generated key a MEDIA answer lists to point at this photo.
    idempotency_key: UUID | None


class AnswerOut(BaseModel):
    question_stable_key: str
    prompt: str | None  # null when the key is no longer in the recorded form version (Ruling 15)
    value: Any
    # "MEDIA" lists photo idempotency keys; null alongside a null prompt.
    question_type: str | None


class ExecutionDetail(ExecutionListItem):
    scans: list[ExecutionScan]
    evidence: list[ExecutionEvidence]
    form_version_id: UUID | None
    answers: list[AnswerOut]


class ResolveReviewRequest(BaseModel):
    note: str | None = None


def _to_item(row: ExecutionListRow) -> ExecutionListItem:
    return ExecutionListItem(
        execution_id=row.execution_id,
        field_worker_id=row.field_worker_id,
        field_worker_name=row.field_worker_name,
        service_point_id=row.service_point_id,
        service_point_name=row.service_point_name,
        building_id=row.building_id,
        building_name=row.building_name,
        floor_label=row.floor_label,
        checked_in_at=row.checked_in_at,
        checked_out_at=row.checked_out_at,
        geo_validation=row.geo_validation,
        review_status=row.review_status,
        validation_flags=row.validation_flags,
        source=row.source,
        evidence_count=row.evidence_count,
        route_id=row.route_id,
        clock_skew_seconds=row.clock_skew_seconds,
        appeal_id=row.appeal_id,
    )


def _to_detail(detail: ExecutionDetailRow) -> ExecutionDetail:
    item = _to_item(detail.execution)
    return ExecutionDetail(
        **item.model_dump(),
        scans=[
            ExecutionScan(
                kind=scan.kind,
                geo_validation=scan.geo_validation,
                latitude=scan.latitude,
                longitude=scan.longitude,
                distance_m=scan.distance_m,
                service_point_id=scan.service_point_id,
                scanned_at=scan.scanned_at,
            )
            for scan in detail.scans
        ],
        evidence=[
            ExecutionEvidence(
                id=item_.id,
                kind=item_.kind,
                text_body=item_.text_body,
                content_type=item_.content_type,
                byte_size=item_.byte_size,
                captured_at=item_.captured_at,
                created_at=item_.created_at,
                content_url=f"/evidence/{item_.id}/content" if item_.kind == "PHOTO" else None,
                idempotency_key=item_.idempotency_key,
            )
            for item_ in detail.evidence
        ],
        form_version_id=detail.form_version_id,
        answers=[
            AnswerOut(
                question_stable_key=a.question_stable_key,
                prompt=a.prompt,
                value=a.value,
                question_type=a.question_type,
            )
            for a in detail.answers
        ],
    )


@router.get("", response_model=PagedExecutions)
async def list_execution_history(
    actor: _Manager,
    db: _Db,
    date_from: date | None = None,
    date_to: date | None = None,
    field_worker_id: UUID | None = None,
    service_point_id: UUID | None = None,
    service_type_id: UUID | None = None,
    building_id: UUID | None = None,
    review_status: str | None = None,
    geo_validation: str | None = None,
    source: str | None = None,
    route_stop_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> PagedExecutions:
    page = max(page, 1)
    page_size = min(max(page_size, 1), _MAX_PAGE_SIZE)
    filters = ExecutionFilters(
        date_from=date_from,
        date_to=date_to,
        field_worker_id=field_worker_id,
        service_point_id=service_point_id,
        service_type_id=service_type_id,
        building_id=building_id,
        review_status=review_status,
        geo_validation=geo_validation,
        source=source,
        route_stop_status=route_stop_status,
    )
    try:
        rows, total = await list_executions(db, filters, page=page, page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # RNF12: filtering the history by a field worker is a targeted read of that person's
    # data by a manager/admin (this router is manager-only). An unfiltered list is not.
    if field_worker_id is not None:
        worker = await db.get(FieldWorker, field_worker_id)
        if worker is not None and worker.user_id is not None:
            await record_personal_data_access(
                db,
                actor_id=actor.id,
                subject_user_id=worker.user_id,
                context="execution_history",
            )
            await db.commit()

    return PagedExecutions(
        items=[_to_item(row) for row in rows], total=total, page=page, page_size=page_size
    )


@router.get("/{execution_id}", response_model=ExecutionDetail)
async def get_execution(execution_id: UUID, actor: _Manager, db: _Db) -> ExecutionDetail:
    detail = await get_execution_detail(db, execution_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Execution "{execution_id}" not found.')
    return _to_detail(detail)


@router.post("/{execution_id}/review/resolve", response_model=ExecutionDetail)
async def resolve_execution_review(
    execution_id: UUID,
    actor: _Manager,
    db: _Db,
    body: ResolveReviewRequest | None = None,
) -> ExecutionDetail:
    try:
        await resolve_review(db, execution_id=execution_id)
    except ExecutionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ReviewNotPending as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    note = body.note if body is not None else None
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="execution",
        entity_id=execution_id,
        action="resolve_review",
        before={"review_status": "PENDING_REVIEW"},
        after={"review_status": "RESOLVED", "note": note},
    )
    await db.commit()

    detail = await get_execution_detail(db, execution_id)
    if detail is None:  # just resolved above; the row must exist
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Execution "{execution_id}" not found.')
    return _to_detail(detail)
