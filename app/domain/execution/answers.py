"""Structural validation of dynamic-form answers carried by a check-out (Ruling 7, Etapa 7).

Pure — no session, no `required` check. The device already gated completeness (RF40); the
server only rejects a payload that is structurally impossible: an unknown `stable_key` or a
value whose JSON shape cannot match the question's `question_type`. The field record
prevails over the form's own rules (Ruling 6)."""

import uuid
from dataclasses import dataclass
from typing import Any

from app.domain.execution.models import Answer
from app.domain.forms.models import FormQuestion, FormVersion, QuestionType


@dataclass(frozen=True)
class AnswerIn:
    stable_key: str
    value: Any


# Named for the signal condition, not "...Error"-suffixed — same convention as
# app/domain/forms/service.py and app/domain/execution/service.py.
class AnswerValidationError(Exception):
    """A check-out answer payload is structurally invalid (Ruling 7): unknown key or bad shape."""


def build_answers(
    *, execution_id: uuid.UUID, form_version: FormVersion, answers: list[AnswerIn]
) -> list[Answer]:
    """Validate each answer against `form_version`'s questions and return unsaved `Answer` rows.

    Ruling 7 structural check only — no `required` check. The caller adds the rows to the
    session. Raises `AnswerValidationError` naming the offending key and the reason.

    Example:
        rows = build_answers(execution_id=e.id, form_version=v, answers=[AnswerIn("k", True)])
        db.add_all(rows)
    """
    questions = {str(q.stable_key): q for q in form_version.questions}
    seen_keys: set[str] = set()
    for answer in answers:
        if answer.stable_key in seen_keys:
            raise AnswerValidationError(
                f'duplicate stable_key "{answer.stable_key}" in answers payload'
            )
        seen_keys.add(answer.stable_key)
        question = questions.get(answer.stable_key)
        if question is None:
            raise AnswerValidationError(
                f'unknown stable_key "{answer.stable_key}" for form version {form_version.id}'
            )
        _check_value_shape(question, answer)
    return [
        Answer(
            execution_id=execution_id,
            question_stable_key=answer.stable_key,
            value_json=answer.value,
        )
        for answer in answers
    ]


def _check_value_shape(question: FormQuestion, answer: AnswerIn) -> None:
    value = answer.value
    key = answer.stable_key
    question_type = question.question_type
    if question_type is QuestionType.BOOLEAN:
        if not isinstance(value, bool):
            raise AnswerValidationError(f'answer for "{key}" must be a boolean, got {value!r}')
    elif question_type is QuestionType.NUMBER:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AnswerValidationError(f'answer for "{key}" must be a number, got {value!r}')
    elif question_type is QuestionType.TEXT:
        if not isinstance(value, str):
            raise AnswerValidationError(f'answer for "{key}" must be a string, got {value!r}')
    elif question_type is QuestionType.SINGLE_CHOICE:
        if not isinstance(value, str) or value not in question.options:
            raise AnswerValidationError(
                f'answer for "{key}" must be one of {question.options!r}, got {value!r}'
            )
    elif question_type is QuestionType.MULTI_CHOICE and not _is_option_subset(
        value, question.options
    ):
        raise AnswerValidationError(
            f'answer for "{key}" must be a subset of {question.options!r}, got {value!r}'
        )


def _is_option_subset(value: object, options: list[str]) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and set(value) <= set(options)
    )
