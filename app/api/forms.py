"""Etapa 7 — the form-builder REST surface: manage the single DRAFT version of a
service type's execution form and publish it immutably (spec §5.1).

Every mutation endpoint owns its transaction boundary — call the flush-only builder
service, append an audit-trail entry, then `db.commit()` — the same discipline as
`app/api/settings.py`. The builder is not the self-committing service exception."""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.forms.models import FormQuestion, FormVersion, FormVersionStatus, QuestionType
from app.domain.forms.reads import to_form_overview, to_form_version_out
from app.domain.forms.service import (
    EmptyDraft,
    FormNotFound,
    InvalidOptions,
    NoDraftVersion,
    QuestionNotInDraft,
    ReorderMismatch,
    add_question,
    get_or_create_form,
    publish_form,
    remove_question,
    reorder_questions,
    update_question,
)
from app.domain.identity.models import User, UserRole

router = APIRouter(tags=["forms"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Builder-service failures the endpoints translate to HTTP status by exact type.
_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    FormNotFound: status.HTTP_404_NOT_FOUND,
    NoDraftVersion: status.HTTP_409_CONFLICT,
    QuestionNotInDraft: status.HTTP_404_NOT_FOUND,
    ReorderMismatch: status.HTTP_422_UNPROCESSABLE_ENTITY,
    EmptyDraft: status.HTTP_422_UNPROCESSABLE_ENTITY,
    InvalidOptions: status.HTTP_422_UNPROCESSABLE_ENTITY,
}


class QuestionBody(BaseModel):
    prompt: str
    question_type: QuestionType
    required: bool
    options: list[str] = []


class ReorderBody(BaseModel):
    stable_keys: list[UUID]


class FormQuestionResponse(BaseModel):
    id: UUID
    stable_key: UUID
    order_index: int
    prompt: str
    question_type: QuestionType
    required: bool
    options: list[str]
    content_hash: str | None


class FormVersionResponse(BaseModel):
    form_version_id: UUID
    status: FormVersionStatus
    version_number: int
    questions: list[FormQuestionResponse]


class PublishedVersionResponse(BaseModel):
    form_version_id: UUID
    version_number: int
    published_at: datetime | None
    question_count: int


class FormOverviewResponse(BaseModel):
    form_id: UUID
    service_type_id: UUID
    draft: FormVersionResponse
    published: list[PublishedVersionResponse]


def _raise_for_domain_error(exc: Exception) -> NoReturn:
    raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc


async def _load_version(db: AsyncSession, version_id: UUID) -> FormVersionResponse:
    """Reproject a version (questions eager-loaded) after the commit that persisted it."""
    version = await db.scalar(
        select(FormVersion)
        .where(FormVersion.id == version_id)
        .options(selectinload(FormVersion.questions))
        .execution_options(populate_existing=True)
    )
    assert version is not None  # just committed by the caller
    return FormVersionResponse(**asdict(to_form_version_out(version)))


async def _load_draft(db: AsyncSession, form_id: UUID) -> FormVersionResponse:
    draft = await db.scalar(
        select(FormVersion)
        .where(FormVersion.form_id == form_id, FormVersion.status == FormVersionStatus.DRAFT)
        .options(selectinload(FormVersion.questions))
        .execution_options(populate_existing=True)
    )
    assert draft is not None  # a form always keeps exactly one DRAFT
    return FormVersionResponse(**asdict(to_form_version_out(draft)))


async def _draft_question_snapshot(
    db: AsyncSession, form_id: UUID, stable_key: UUID
) -> dict[str, object] | None:
    """A small pre-change dict for the audit `before`, or None when the key is absent
    (the service then raises the precise error the endpoint maps)."""
    question = await db.scalar(
        select(FormQuestion)
        .join(FormVersion, FormVersion.id == FormQuestion.form_version_id)
        .where(
            FormVersion.form_id == form_id,
            FormVersion.status == FormVersionStatus.DRAFT,
            FormQuestion.stable_key == stable_key,
        )
    )
    if question is None:
        return None
    return {
        "stable_key": str(question.stable_key),
        "prompt": question.prompt,
        "question_type": question.question_type.value,
        "required": question.required,
        "options": list(question.options),
    }


@router.get("/service-types/{service_type_id}/form", response_model=FormOverviewResponse)
async def get_form(service_type_id: UUID, _actor: _Manager, db: _Db) -> FormOverviewResponse:
    try:
        await get_or_create_form(db, service_type_id=service_type_id)
    except FormNotFound as exc:
        _raise_for_domain_error(exc)
    await db.commit()
    form = await get_or_create_form(db, service_type_id=service_type_id)
    return FormOverviewResponse(**asdict(to_form_overview(form)))


@router.post(
    "/forms/{form_id}/draft/questions",
    response_model=FormVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_question(
    form_id: UUID, body: QuestionBody, actor: _Manager, db: _Db
) -> FormVersionResponse:
    try:
        question = await add_question(
            db,
            form_id=form_id,
            prompt=body.prompt,
            question_type=body.question_type,
            required=body.required,
            options=body.options,
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="form",
        entity_id=form_id,
        action="add_question",
        before=None,
        after={
            "stable_key": str(question.stable_key),
            "prompt": question.prompt,
            "question_type": question.question_type.value,
            "required": question.required,
            "options": list(question.options),
        },
    )
    await db.commit()
    return await _load_draft(db, form_id)


@router.patch("/forms/{form_id}/draft/questions/{stable_key}", response_model=FormVersionResponse)
async def patch_question(
    form_id: UUID, stable_key: UUID, body: QuestionBody, actor: _Manager, db: _Db
) -> FormVersionResponse:
    before = await _draft_question_snapshot(db, form_id, stable_key)
    try:
        question = await update_question(
            db,
            form_id=form_id,
            stable_key=stable_key,
            prompt=body.prompt,
            question_type=body.question_type,
            required=body.required,
            options=body.options,
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="form",
        entity_id=form_id,
        action="update_question",
        before=before,
        after={
            "stable_key": str(question.stable_key),
            "prompt": question.prompt,
            "question_type": question.question_type.value,
            "required": question.required,
            "options": list(question.options),
        },
    )
    await db.commit()
    return await _load_draft(db, form_id)


@router.delete("/forms/{form_id}/draft/questions/{stable_key}", response_model=FormVersionResponse)
async def delete_question(
    form_id: UUID, stable_key: UUID, actor: _Manager, db: _Db
) -> FormVersionResponse:
    before = await _draft_question_snapshot(db, form_id, stable_key)
    try:
        await remove_question(db, form_id=form_id, stable_key=stable_key)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="form",
        entity_id=form_id,
        action="remove_question",
        before=before,
        after=None,
    )
    await db.commit()
    return await _load_draft(db, form_id)


@router.put("/forms/{form_id}/draft/order", response_model=FormVersionResponse)
async def put_order(
    form_id: UUID, body: ReorderBody, actor: _Manager, db: _Db
) -> FormVersionResponse:
    before_draft = await db.scalar(
        select(FormVersion)
        .where(FormVersion.form_id == form_id, FormVersion.status == FormVersionStatus.DRAFT)
        .options(selectinload(FormVersion.questions))
    )
    before_order = (
        [str(q.stable_key) for q in before_draft.questions] if before_draft is not None else None
    )
    try:
        await reorder_questions(db, form_id=form_id, stable_keys=body.stable_keys)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="form",
        entity_id=form_id,
        action="reorder_questions",
        before={"order": before_order},
        after={"order": [str(key) for key in body.stable_keys]},
    )
    await db.commit()
    return await _load_draft(db, form_id)


@router.post(
    "/forms/{form_id}/publish",
    response_model=FormVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish(form_id: UUID, actor: _Manager, db: _Db) -> FormVersionResponse:
    try:
        version = await publish_form(db, form_id=form_id)
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        _raise_for_domain_error(exc)
    published_id = version.id
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="form",
        entity_id=form_id,
        action="publish_form",
        before=None,
        after={
            "form_version_id": str(version.id),
            "version_number": version.version_number,
            "question_count": len(version.questions),
        },
    )
    await db.commit()
    return await _load_version(db, published_id)
