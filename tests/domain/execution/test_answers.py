"""Ruling 7 structural validation for check-out answers — pure, no session."""

import uuid

import pytest
from app.domain.execution.answers import AnswerIn, AnswerValidationError, build_answers
from app.domain.forms.models import FormQuestion, FormVersion, QuestionType


def _question(question_type: QuestionType, *, options: list[str] | None = None) -> FormQuestion:
    return FormQuestion(
        stable_key=uuid.uuid4(),
        order_index=0,
        prompt="p",
        question_type=question_type,
        required=True,
        options=options or [],
        content_hash=None,
    )


def _version(questions: list[FormQuestion]) -> FormVersion:
    version = FormVersion(id=uuid.uuid4(), form_id=uuid.uuid4(), version_number=1)
    version.questions = questions
    return version


def test_valid_mixed_answers_preserve_value_json() -> None:
    text_q = _question(QuestionType.TEXT)
    number_q = _question(QuestionType.NUMBER)
    bool_q = _question(QuestionType.BOOLEAN)
    single_q = _question(QuestionType.SINGLE_CHOICE, options=["a", "b"])
    multi_q = _question(QuestionType.MULTI_CHOICE, options=["x", "y", "z"])
    version = _version([text_q, number_q, bool_q, single_q, multi_q])
    execution_id = uuid.uuid4()

    answers = build_answers(
        execution_id=execution_id,
        form_version=version,
        answers=[
            AnswerIn(str(text_q.stable_key), "hello"),
            AnswerIn(str(number_q.stable_key), 3.5),
            AnswerIn(str(bool_q.stable_key), True),
            AnswerIn(str(single_q.stable_key), "b"),
            AnswerIn(str(multi_q.stable_key), ["x", "z"]),
        ],
    )

    assert [a.value_json for a in answers] == ["hello", 3.5, True, "b", ["x", "z"]]
    assert all(a.execution_id == execution_id for a in answers)
    assert {a.question_stable_key for a in answers} == {
        str(q.stable_key) for q in version.questions
    }


def test_unknown_stable_key_rejected() -> None:
    version = _version([_question(QuestionType.TEXT)])
    with pytest.raises(AnswerValidationError, match="unknown stable_key"):
        build_answers(
            execution_id=uuid.uuid4(),
            form_version=version,
            answers=[AnswerIn("not-a-key", "x")],
        )


def test_single_choice_value_not_in_options_rejected() -> None:
    q = _question(QuestionType.SINGLE_CHOICE, options=["a", "b"])
    version = _version([q])
    with pytest.raises(AnswerValidationError, match="must be one of"):
        build_answers(
            execution_id=uuid.uuid4(),
            form_version=version,
            answers=[AnswerIn(str(q.stable_key), "c")],
        )


def test_boolean_question_with_string_value_rejected() -> None:
    q = _question(QuestionType.BOOLEAN)
    version = _version([q])
    with pytest.raises(AnswerValidationError, match="must be a boolean"):
        build_answers(
            execution_id=uuid.uuid4(),
            form_version=version,
            answers=[AnswerIn(str(q.stable_key), "true")],
        )


def test_number_question_rejects_bool() -> None:
    q = _question(QuestionType.NUMBER)
    version = _version([q])
    with pytest.raises(AnswerValidationError, match="must be a number"):
        build_answers(
            execution_id=uuid.uuid4(),
            form_version=version,
            answers=[AnswerIn(str(q.stable_key), True)],
        )


def test_multi_choice_rejects_value_outside_options() -> None:
    q = _question(QuestionType.MULTI_CHOICE, options=["x", "y"])
    version = _version([q])
    with pytest.raises(AnswerValidationError, match="must be a subset"):
        build_answers(
            execution_id=uuid.uuid4(),
            form_version=version,
            answers=[AnswerIn(str(q.stable_key), ["x", "w"])],
        )


def test_required_question_omitted_is_not_an_error() -> None:
    answered = _question(QuestionType.TEXT)
    _required_but_omitted = _question(QuestionType.BOOLEAN)
    version = _version([answered, _required_but_omitted])

    rows = build_answers(
        execution_id=uuid.uuid4(),
        form_version=version,
        answers=[AnswerIn(str(answered.stable_key), "only this one")],
    )

    assert len(rows) == 1
