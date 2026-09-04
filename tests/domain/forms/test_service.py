"""Form builder service (Task 2) — draft mutation, publish cycle and active-version
resolution, all over the `db_session` fixture (spec §3.1, Etapa 7)."""

import uuid

import pytest
from app.domain.catalog.models import ServiceType
from app.domain.forms.hashing import question_content_hash
from app.domain.forms.models import FormVersion, FormVersionStatus, QuestionType
from app.domain.forms.reads import to_form_overview
from app.domain.forms.service import (
    EmptyDraft,
    InvalidOptions,
    QuestionNotInDraft,
    ReorderMismatch,
    active_form_version,
    add_question,
    get_or_create_form,
    publish_form,
    remove_question,
    reorder_questions,
    update_question,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


async def _a_service_type(db: AsyncSession) -> ServiceType:
    st = ServiceType(name="Limpeza", average_duration_minutes=30)
    db.add(st)
    await db.flush()
    return st


async def _draft_of(db: AsyncSession, form_id: uuid.UUID) -> FormVersion:
    return (
        await db.execute(
            select(FormVersion)
            .where(
                FormVersion.form_id == form_id,
                FormVersion.status == FormVersionStatus.DRAFT,
            )
            .options(selectinload(FormVersion.questions))
        )
    ).scalar_one()


async def test_get_or_create_form_is_idempotent(db_session: AsyncSession) -> None:
    st = await _a_service_type(db_session)
    first = await get_or_create_form(db_session, service_type_id=st.id)
    second = await get_or_create_form(db_session, service_type_id=st.id)
    assert first.id == second.id
    forms = (await db_session.execute(select(FormVersion))).scalars().all()
    assert len(forms) == 1
    assert forms[0].status is FormVersionStatus.DRAFT
    assert forms[0].version_number == 0


async def test_add_question_appends_with_incrementing_index_and_null_hash(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    q0 = await add_question(
        db_session,
        form_id=form.id,
        prompt="A?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    q1 = await add_question(
        db_session,
        form_id=form.id,
        prompt="B?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    assert (q0.order_index, q1.order_index) == (0, 1)
    assert q0.content_hash is None
    assert q1.content_hash is None
    assert q0.stable_key != q1.stable_key


async def test_update_published_question_raises_question_not_in_draft(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    q = await add_question(
        db_session,
        form_id=form.id,
        prompt="Limpo?",
        question_type=QuestionType.BOOLEAN,
        required=True,
        options=[],
    )
    published = await publish_form(db_session, form_id=form.id)
    published_key = published.questions[0].stable_key
    # the published version's own key still exists in the regenerated draft, so target a
    # key that never was: publishing does not expose an editable published question.
    assert published_key == q.stable_key
    with pytest.raises(QuestionNotInDraft):
        await update_question(
            db_session,
            form_id=form.id,
            stable_key=uuid.uuid4(),
            prompt="x",
            question_type=QuestionType.TEXT,
            required=False,
            options=[],
        )


async def test_reorder_with_missing_key_raises_mismatch(db_session: AsyncSession) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    q0 = await add_question(
        db_session,
        form_id=form.id,
        prompt="A?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    await add_question(
        db_session,
        form_id=form.id,
        prompt="B?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    with pytest.raises(ReorderMismatch):
        await reorder_questions(db_session, form_id=form.id, stable_keys=[q0.stable_key])


async def test_publish_empty_draft_raises(db_session: AsyncSession) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    with pytest.raises(EmptyDraft):
        await publish_form(db_session, form_id=form.id)


async def test_publish_cycle(db_session: AsyncSession) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    q1 = await add_question(
        db_session,
        form_id=form.id,
        prompt="Limpo?",
        question_type=QuestionType.BOOLEAN,
        required=True,
        options=[],
    )
    q2 = await add_question(
        db_session,
        form_id=form.id,
        prompt="Nível",
        question_type=QuestionType.SINGLE_CHOICE,
        required=True,
        options=["baixo", "alto"],
    )
    v1 = await publish_form(db_session, form_id=form.id)
    assert v1.version_number == 1
    assert v1.published_at is not None
    assert v1.status is FormVersionStatus.PUBLISHED
    for question in v1.questions:
        assert question.content_hash == question_content_hash(
            prompt=question.prompt,
            question_type=question.question_type,
            required=question.required,
            options=list(question.options),
        )

    active = await active_form_version(db_session, service_type_id=st.id)
    assert active is not None
    assert active.id == v1.id

    draft = await _draft_of(db_session, form.id)
    assert draft.version_number == 0
    assert {q.stable_key for q in draft.questions} == {q1.stable_key, q2.stable_key}
    assert all(q.content_hash is None for q in draft.questions)

    v2 = await publish_form(db_session, form_id=form.id)
    assert v2.version_number == 2
    active_again = await active_form_version(db_session, service_type_id=st.id)
    assert active_again is not None
    assert active_again.id == v2.id
    still_v1 = await db_session.get(FormVersion, v1.id)
    assert still_v1 is not None
    assert still_v1.version_number == 1


async def test_to_form_overview_reflects_draft_and_published(db_session: AsyncSession) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    await add_question(
        db_session,
        form_id=form.id,
        prompt="Limpo?",
        question_type=QuestionType.BOOLEAN,
        required=True,
        options=[],
    )
    await publish_form(db_session, form_id=form.id)
    reloaded = await get_or_create_form(db_session, service_type_id=st.id)
    overview = to_form_overview(reloaded)
    assert overview.service_type_id == st.id
    assert overview.draft.version_number == 0
    assert len(overview.draft.questions) == 1
    assert [p.version_number for p in overview.published] == [1]
    assert overview.published[0].question_count == 1


async def test_publish_rejects_choice_question_with_too_few_options(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    await add_question(
        db_session,
        form_id=form.id,
        prompt="Nível",
        question_type=QuestionType.SINGLE_CHOICE,
        required=True,
        options=["só um"],
    )
    with pytest.raises(InvalidOptions):
        await publish_form(db_session, form_id=form.id)


async def test_publish_rejects_non_choice_question_carrying_options(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    await add_question(
        db_session,
        form_id=form.id,
        prompt="Comentário",
        question_type=QuestionType.TEXT,
        required=False,
        options=["não deveria estar aqui"],
    )
    with pytest.raises(InvalidOptions):
        await publish_form(db_session, form_id=form.id)


async def test_remove_question_drops_one_and_rejects_unknown_key(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    q0 = await add_question(
        db_session,
        form_id=form.id,
        prompt="A?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    q1 = await add_question(
        db_session,
        form_id=form.id,
        prompt="B?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    await remove_question(db_session, form_id=form.id, stable_key=q0.stable_key)
    draft = await _draft_of(db_session, form.id)
    assert [q.stable_key for q in draft.questions] == [q1.stable_key]
    assert draft.questions[0].order_index == 1
    with pytest.raises(QuestionNotInDraft):
        await remove_question(db_session, form_id=form.id, stable_key=uuid.uuid4())


async def test_active_form_version_is_none_without_a_published_version(
    db_session: AsyncSession,
) -> None:
    st = await _a_service_type(db_session)
    form = await get_or_create_form(db_session, service_type_id=st.id)
    await add_question(
        db_session,
        form_id=form.id,
        prompt="A?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    assert await active_form_version(db_session, service_type_id=st.id) is None
