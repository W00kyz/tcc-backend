"""RF32 — read/write the singleton tolerance-radius settings row.

Transaction discipline: these functions flush, never commit. The endpoint owns the
transaction boundary (spec §7 — "service flushes, never commits")."""

from collections.abc import Mapping
from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.settings.models import SystemSettings

_DEFAULT_RADIUS_M = 50

# Every column type an admin can edit on the settings row.
SettingValue = int | float | time | list[int]


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
    db: AsyncSession, *, actor_id: UUID, changes: Mapping[str, SettingValue]
) -> SystemSettings:
    """Write only the fields in `changes` (an absent field is left unchanged), then stamp the
    actor and timestamp. Flushes; the endpoint owns the commit.

        await update_settings(db, actor_id=admin.id, changes={"check_radius_meters": 80})
    """
    row = await get_settings_row(db)
    for field, value in changes.items():
        setattr(row, field, value)
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row
