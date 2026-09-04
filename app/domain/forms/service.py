# ruff: noqa: N818 — FormNotFound / NoDraftVersion / QuestionNotInDraft / ReorderMismatch /
# EmptyDraft / InvalidOptions name the signal condition the builder router (Task 3) catches by
# name, same convention as app/domain/routing/service.py — not "...Error"-suffixed generics.
"""Form builder operations: mutate the single DRAFT version, publish it immutably, and
resolve the active PUBLISHED version for a service type (spec §3.1, Etapa 7).

Transaction discipline: these functions flush, never commit — the endpoint owns the
transaction boundary (spec §7). `app/domain/execution/service.py` is the only documented
exception to that rule; this module is not."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import ServiceType
from app.domain.forms.hashing import question_content_hash
from app.domain.forms.models import (
    Form,
    FormQuestion,
    FormVersion,
    FormVersionStatus,
    QuestionType,
)

_CHOICE_TYPES = {QuestionType.SINGLE_CHOICE, QuestionType.MULTI_CHOICE}


class FormError(Exception):
    """Base class for every builder-service failure."""


class FormNotFound(FormError):
    pass


class NoDraftVersion(FormError):
    pass


class QuestionNotInDraft(FormError):
    pass


class ReorderMismatch(FormError):
    pass


class EmptyDraft(FormError):
    pass


class InvalidOptions(FormError):
    pass


async def get_or_create_form(db: AsyncSession, *, service_type_id: uuid.UUID) -> Form:
    """Return the form for a service type, creating it with an empty DRAFT version on first use."""
    form = await db.scalar(
        select(Form)
        .where(Form.service_type_id == service_type_id)
        .options(selectinload(Form.versions).selectinload(FormVersion.questions))
        .execution_options(populate_existing=True)
    )
    if form is not None:
        return form
    service_type = await db.get(ServiceType, service_type_id)
    if service_type is None:
        raise FormNotFound(f"No service type with id {service_type_id}; expected an existing row.")
    form = Form(service_type_id=service_type_id, name=service_type.name)
    form.versions.append(FormVersion(status=FormVersionStatus.DRAFT, version_number=0))
    db.add(form)
    await db.flush()
    return form


async def add_question(
    db: AsyncSession,
    *,
    form_id: uuid.UUID,
    prompt: str,
    question_type: QuestionType,
    required: bool,
    options: list[str],
) -> FormQuestion:
    """Append a question to the form's DRAFT version with a fresh stable_key and no hash."""
    draft = await _draft_version(db, form_id)
    next_index = max((q.order_index for q in draft.questions), default=-1) + 1
    question = FormQuestion(
        form_version_id=draft.id,
        stable_key=uuid.uuid4(),
        order_index=next_index,
        prompt=prompt,
        question_type=question_type,
        required=required,
        options=list(options),
        content_hash=None,
    )
    db.add(question)
    await db.flush()
    return question


async def update_question(
    db: AsyncSession,
    *,
    form_id: uuid.UUID,
    stable_key: uuid.UUID,
    prompt: str,
    question_type: QuestionType,
    required: bool,
    options: list[str],
) -> FormQuestion:
    """Edit a DRAFT question in place. Raises QuestionNotInDraft for a non-draft key."""
    question = await _draft_question(db, form_id, stable_key)
    question.prompt = prompt
    question.question_type = question_type
    question.required = required
    question.options = list(options)
    await db.flush()
    return question


async def remove_question(db: AsyncSession, *, form_id: uuid.UUID, stable_key: uuid.UUID) -> None:
    """Delete a question from the DRAFT version. Raises QuestionNotInDraft for a non-draft key."""
    draft = await _draft_version(db, form_id)
    question = next((q for q in draft.questions if q.stable_key == stable_key), None)
    if question is None:
        raise QuestionNotInDraft(f"Question {stable_key} is not in form {form_id}'s DRAFT version.")
    # drop it from the loaded collection too, or the save-update cascade re-persists the row.
    draft.questions.remove(question)
    await db.delete(question)
    await db.flush()


async def reorder_questions(
    db: AsyncSession, *, form_id: uuid.UUID, stable_keys: list[uuid.UUID]
) -> list[FormQuestion]:
    """Rewrite order_index to 0..n-1 in the given order.

    Raises ReorderMismatch unless stable_keys is a permutation of the DRAFT's current keys."""
    draft = await _draft_version(db, form_id)
    by_key = {q.stable_key: q for q in draft.questions}
    if set(stable_keys) != set(by_key):
        raise ReorderMismatch(
            f"Reorder keys {sorted(map(str, stable_keys))} do not match the DRAFT's "
            f"{sorted(map(str, by_key))}."
        )
    for index, key in enumerate(stable_keys):
        by_key[key].order_index = index
    await db.flush()
    return [by_key[key] for key in stable_keys]


async def publish_form(db: AsyncSession, *, form_id: uuid.UUID) -> FormVersion:
    """Validate the DRAFT, flip it to PUBLISHED with computed hashes, and open a fresh DRAFT
    cloning the just-published questions (same stable_key, hashes cleared)."""
    draft = await _draft_version(db, form_id)
    _validate_draft(draft.questions)
    highest = await db.scalar(
        select(func.max(FormVersion.version_number)).where(
            FormVersion.form_id == form_id,
            FormVersion.status == FormVersionStatus.PUBLISHED,
        )
    )
    draft.status = FormVersionStatus.PUBLISHED
    draft.version_number = (highest or 0) + 1
    draft.published_at = datetime.now(UTC)
    for question in draft.questions:
        question.content_hash = question_content_hash(
            prompt=question.prompt,
            question_type=question.question_type,
            required=question.required,
            options=list(question.options),
        )
    await db.flush()
    await _clone_as_draft(db, draft)
    return draft


async def active_form_version(
    db: AsyncSession, *, service_type_id: uuid.UUID
) -> FormVersion | None:
    """The highest-numbered PUBLISHED version for the service type's form, questions eager-loaded.

    Returns None when the type has no form or no published version yet."""
    version: FormVersion | None = await db.scalar(
        select(FormVersion)
        .join(Form, Form.id == FormVersion.form_id)
        .where(
            Form.service_type_id == service_type_id,
            FormVersion.status == FormVersionStatus.PUBLISHED,
        )
        .order_by(FormVersion.version_number.desc())
        .limit(1)
        .options(selectinload(FormVersion.questions))
        .execution_options(populate_existing=True)
    )
    return version


def _validate_draft(questions: list[FormQuestion]) -> None:
    if not questions:
        raise EmptyDraft("Cannot publish a DRAFT with no questions; expected at least one.")
    for question in questions:
        is_choice = question.question_type in _CHOICE_TYPES
        if is_choice and len(question.options) < 2:
            raise InvalidOptions(
                f"Choice question {question.stable_key} needs >= 2 options, "
                f"got {question.options!r}."
            )
        if not is_choice and question.options != []:
            raise InvalidOptions(
                f"Non-choice question {question.stable_key} must have no options, "
                f"got {question.options!r}."
            )


async def _clone_as_draft(db: AsyncSession, published: FormVersion) -> FormVersion:
    draft = FormVersion(form_id=published.form_id, status=FormVersionStatus.DRAFT, version_number=0)
    draft.questions = [
        FormQuestion(
            stable_key=q.stable_key,
            order_index=q.order_index,
            prompt=q.prompt,
            question_type=q.question_type,
            required=q.required,
            options=list(q.options),
            content_hash=None,
        )
        for q in published.questions
    ]
    db.add(draft)
    await db.flush()
    return draft


async def _draft_version(db: AsyncSession, form_id: uuid.UUID) -> FormVersion:
    version = await db.scalar(
        select(FormVersion)
        .where(
            FormVersion.form_id == form_id,
            FormVersion.status == FormVersionStatus.DRAFT,
        )
        .options(selectinload(FormVersion.questions))
        # refresh the cached instance + its questions collection: callers mutate rows within
        # the same session (add_question, then publish_form) and must see those writes.
        .execution_options(populate_existing=True)
    )
    if version is not None:
        return version
    if await db.scalar(select(Form.id).where(Form.id == form_id)) is None:
        raise FormNotFound(f"No form with id {form_id}; expected an existing form.")
    raise NoDraftVersion(f"Form {form_id} has no DRAFT version.")


async def _draft_question(
    db: AsyncSession, form_id: uuid.UUID, stable_key: uuid.UUID
) -> FormQuestion:
    draft = await _draft_version(db, form_id)
    question = next((q for q in draft.questions if q.stable_key == stable_key), None)
    if question is None:
        raise QuestionNotInDraft(f"Question {stable_key} is not in form {form_id}'s DRAFT version.")
    return question
