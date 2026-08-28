import uuid

from app.domain.audit.service import record_audit_trail
from sqlalchemy.ext.asyncio import AsyncSession


async def test_record_audit_trail_persists_before_and_after(db_session: AsyncSession) -> None:
    actor_id = uuid.uuid4()
    entity_id = uuid.uuid4()

    entry = await record_audit_trail(
        db_session,
        actor_id=actor_id,
        entity_type="service_point",
        entity_id=entity_id,
        action="UPDATE",
        before={"name": "Sala antiga"},
        after={"name": "Sala nova"},
    )
    await db_session.commit()

    assert entry.id is not None
    assert entry.actor_id == actor_id
    assert entry.entity_type == "service_point"
    assert entry.before_data == {"name": "Sala antiga"}
    assert entry.after_data == {"name": "Sala nova"}


async def test_record_audit_trail_allows_system_actor(db_session: AsyncSession) -> None:
    # Purge jobs, automatic alerts etc. have no user — actor_id is optional.
    entry = await record_audit_trail(
        db_session,
        actor_id=None,
        entity_type="worker_position",
        entity_id=uuid.uuid4(),
        action="PURGE",
        before={"retained_days": 90},
        after=None,
    )
    await db_session.commit()

    assert entry.actor_id is None
    assert entry.after_data is None
