import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditTrail


async def record_audit_trail(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> AuditTrail:
    """Append one audit entry. Caller commits — this stays a pure write, no transaction
    boundary of its own, so it composes inside the caller's own unit of work."""
    entry = AuditTrail(
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_data=before,
        after_data=after,
    )
    db.add(entry)
    await db.flush()
    return entry


async def record_personal_data_access(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    subject_user_id: uuid.UUID,
    context: str,
) -> None:
    """RNF12 — one audit row when a manager/admin reads another person's personal data.

    Example: ``await record_personal_data_access(db, actor_id=caller.id,
    subject_user_id=worker.user_id, context="execution_history")``.
    """
    await record_audit_trail(
        db,
        actor_id=actor_id,
        entity_type="personal_data",
        entity_id=subject_user_id,
        action="PERSONAL_DATA_ACCESS",
        before=None,
        after={"context": context},
    )
