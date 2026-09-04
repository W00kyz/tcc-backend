"""Read-side projections of the form builder — frozen shapes the router (Task 3) returns
without importing the mutation surface (spec §3.1, Etapa 7)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.forms.models import (
    Form,
    FormQuestion,
    FormVersion,
    FormVersionStatus,
    QuestionType,
)


@dataclass(frozen=True)
class FormQuestionOut:
    id: uuid.UUID
    stable_key: uuid.UUID
    order_index: int
    prompt: str
    question_type: QuestionType
    required: bool
    options: list[str]
    content_hash: str | None


@dataclass(frozen=True)
class FormVersionOut:
    form_version_id: uuid.UUID
    status: FormVersionStatus
    version_number: int
    questions: list[FormQuestionOut]


@dataclass(frozen=True)
class PublishedVersionSummary:
    form_version_id: uuid.UUID
    version_number: int
    published_at: datetime | None
    question_count: int


@dataclass(frozen=True)
class FormOverview:
    form_id: uuid.UUID
    service_type_id: uuid.UUID
    draft: FormVersionOut
    published: list[PublishedVersionSummary]


def _question_out(question: FormQuestion) -> FormQuestionOut:
    return FormQuestionOut(
        id=question.id,
        stable_key=question.stable_key,
        order_index=question.order_index,
        prompt=question.prompt,
        question_type=question.question_type,
        required=question.required,
        options=list(question.options),
        content_hash=question.content_hash,
    )


def to_form_version_out(version: FormVersion) -> FormVersionOut:
    """Project a loaded version (questions eager-loaded) into its frozen read shape.

    Example: `to_form_version_out(draft)` → `FormVersionOut(version_number=0, questions=[...])`.
    """
    return FormVersionOut(
        form_version_id=version.id,
        status=version.status,
        version_number=version.version_number,
        questions=[_question_out(q) for q in version.questions],
    )


def to_form_overview(form: Form) -> FormOverview:
    """Builder landing view: the single DRAFT plus a summary of every PUBLISHED version.

    Example: right after `get_or_create_form`, `to_form_overview(form)` returns a `draft`
    with no questions and `published == []`. Requires `form.versions` (and their questions)
    eager-loaded."""
    draft = next(v for v in form.versions if v.status is FormVersionStatus.DRAFT)
    published = [
        PublishedVersionSummary(
            form_version_id=v.id,
            version_number=v.version_number,
            published_at=v.published_at,
            question_count=len(v.questions),
        )
        for v in form.versions
        if v.status is FormVersionStatus.PUBLISHED
    ]
    return FormOverview(
        form_id=form.id,
        service_type_id=form.service_type_id,
        draft=to_form_version_out(draft),
        published=published,
    )
