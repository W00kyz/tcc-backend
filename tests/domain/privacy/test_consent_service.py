"""Etapa 11 (RNF10): the consent service — current notice lookup, per-user consent
check against the current notice, and idempotent recording."""

from datetime import UTC, datetime

import pytest
from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from app.domain.privacy.models import ConsentRecord, PrivacyNotice
from app.domain.privacy.service import (
    NoCurrentNotice,
    current_notice,
    has_consented,
    record_consent,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_notice(
    db_session: AsyncSession, *, version: int, is_current: bool
) -> PrivacyNotice:
    notice = PrivacyNotice(
        version=version,
        title=f"Aviso v{version}",
        body_markdown="corpo",
        published_at=datetime.now(UTC),
        is_current=is_current,
    )
    db_session.add(notice)
    await db_session.flush()
    return notice


async def _seed_user(db_session: AsyncSession) -> User:
    user = User(
        name="Consent Tester",
        email="consent@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_current_notice_returns_the_current_row(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM privacy_notices"))
    await _seed_notice(db_session, version=1, is_current=False)
    await _seed_notice(db_session, version=2, is_current=True)

    assert (await current_notice(db_session)).version == 2


async def test_current_notice_raises_when_none_is_current(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM privacy_notices"))
    await _seed_notice(db_session, version=1, is_current=False)

    with pytest.raises(NoCurrentNotice):
        await current_notice(db_session)


async def test_has_consented_tracks_only_the_current_notice(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM privacy_notices"))
    old = await _seed_notice(db_session, version=1, is_current=False)
    await _seed_notice(db_session, version=2, is_current=True)
    user = await _seed_user(db_session)

    assert await has_consented(db_session, user_id=user.id) is False

    # A consent against the superseded notice does not count.
    db_session.add(
        ConsentRecord(
            user_id=user.id,
            privacy_notice_id=old.id,
            accepted_at=datetime.now(UTC),
            ip_address=None,
            user_agent=None,
        )
    )
    await db_session.flush()
    assert await has_consented(db_session, user_id=user.id) is False

    await record_consent(db_session, user_id=user.id, ip_address="127.0.0.1", user_agent="pytest")
    assert await has_consented(db_session, user_id=user.id) is True


async def test_record_consent_is_idempotent(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM privacy_notices"))
    await _seed_notice(db_session, version=1, is_current=True)
    user = await _seed_user(db_session)

    await record_consent(db_session, user_id=user.id, ip_address=None, user_agent=None)
    await record_consent(db_session, user_id=user.id, ip_address="10.0.0.1", user_agent="again")

    count = await db_session.scalar(
        select(func.count()).select_from(ConsentRecord).where(ConsentRecord.user_id == user.id)
    )
    assert count == 1
