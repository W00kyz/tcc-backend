"""LGPD (RNF10) consent gate: which privacy notice is current, whether a user has
accepted it, and recording an acceptance. The migration guarantees exactly one current
notice, so `NoCurrentNotice` is a should-never-happen guard, not a routine branch.

    required = not await has_consented(db, user_id=user.id)
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.privacy.models import ConsentRecord, PrivacyNotice


class NoCurrentNotice(Exception):  # noqa: N818 — a should-never-happen guard, let it 500
    """No privacy notice has `is_current = true` — the seed migration should prevent this."""


async def current_notice(db: AsyncSession) -> PrivacyNotice:
    notice = await db.scalar(select(PrivacyNotice).where(PrivacyNotice.is_current.is_(True)))
    if notice is None:
        raise NoCurrentNotice(
            "No privacy notice has is_current = true; expected exactly one (seeded by migration)."
        )
    return notice


async def has_consented(db: AsyncSession, *, user_id: uuid.UUID) -> bool:
    consented = await db.scalar(
        select(
            exists().where(
                ConsentRecord.user_id == user_id,
                ConsentRecord.privacy_notice_id == PrivacyNotice.id,
                PrivacyNotice.is_current.is_(True),
            )
        )
    )
    return bool(consented)


async def record_consent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Idempotent: a repeat acceptance of the same notice is a no-op (ON CONFLICT DO NOTHING
    on uq_consent_user_notice). Caller commits."""
    notice = await current_notice(db)
    stmt = (
        pg_insert(ConsentRecord)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            privacy_notice_id=notice.id,
            accepted_at=datetime.now(UTC),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        .on_conflict_do_nothing(constraint="uq_consent_user_notice")
    )
    await db.execute(stmt)
