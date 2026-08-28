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
