"""RF32 — read/write the singleton tolerance-radius settings row.

Transaction discipline: these functions flush, never commit. The endpoint owns the
transaction boundary (spec §7 — "service flushes, never commits")."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.settings.models import SystemSettings

_DEFAULT_RADIUS_M = 50


async def get_settings_row(db: AsyncSession) -> SystemSettings:
    """Return the singleton settings row. The Etapa 5 migration seeds it, but tests truncate
    every table between cases, so recreate it defensively with the default radius when absent."""
    row = await db.scalar(select(SystemSettings))
    if row is None:
        row = SystemSettings(id=True, check_radius_meters=_DEFAULT_RADIUS_M)
        db.add(row)
        await db.flush()
    return row


async def update_settings(
    db: AsyncSession, *, actor_id: UUID, check_radius_meters: int
) -> SystemSettings:
    row = await get_settings_row(db)
    row.check_radius_meters = check_radius_meters
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row
