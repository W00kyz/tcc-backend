import uuid

import pytest
from app.domain.catalog.models import ServiceType
from app.domain.forms.models import (
    Form,
    FormQuestion,
    FormVersion,
    FormVersionStatus,
    QuestionType,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop


@pytest.mark.asyncio
async def test_form_version_question_round_trip(db_session: AsyncSession) -> None:
    await seed_route_with_one_stop(db_session)
    # seed_route_with_one_stop does not create a ServiceType — make one inline.
    st = ServiceType(name="Limpeza", average_duration_minutes=30)
    db_session.add(st)
    await db_session.flush()

    form = Form(service_type_id=st.id, name="Inspeção de limpeza")
    db_session.add(form)
    await db_session.flush()

    version = FormVersion(form_id=form.id, status=FormVersionStatus.DRAFT, version_number=0)
    db_session.add(version)
    await db_session.flush()

    q = FormQuestion(
        form_version_id=version.id,
        stable_key=uuid.uuid4(),
        order_index=0,
        prompt="Área limpa?",
        question_type=QuestionType.BOOLEAN,
        required=True,
        options=[],
    )
    db_session.add(q)
    await db_session.flush()

    reloaded = await db_session.get(FormQuestion, q.id)
    assert reloaded is not None
    assert reloaded.options == []
    assert reloaded.content_hash is None

    dup = FormQuestion(
        form_version_id=version.id,
        stable_key=q.stable_key,
        order_index=1,
        prompt="x",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_form_service_type_id_is_unique(db_session: AsyncSession) -> None:
    st = ServiceType(name="Jardinagem", average_duration_minutes=45)
    db_session.add(st)
    await db_session.flush()

    db_session.add(Form(service_type_id=st.id, name="A"))
    await db_session.flush()
    db_session.add(Form(service_type_id=st.id, name="B"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
