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
    db: AsyncSession,
    *,
    actor_id: UUID,
    check_radius_meters: int | None = None,
    alert_scan_interval_seconds: int | None = None,
    late_start_tolerance_min: int | None = None,
    absence_tolerance_min: int | None = None,
    late_stop_tolerance_min: int | None = None,
    no_evidence_grace_min: int | None = None,
    position_report_interval_seconds: int | None = None,
) -> SystemSettings:
    """Write only the fields the caller passed (a None means "leave unchanged"), then stamp
    the actor and timestamp. Flushes; the endpoint owns the commit."""
    row = await get_settings_row(db)
    updates: dict[str, int | None] = {
        "check_radius_meters": check_radius_meters,
        "alert_scan_interval_seconds": alert_scan_interval_seconds,
        "late_start_tolerance_min": late_start_tolerance_min,
        "absence_tolerance_min": absence_tolerance_min,
        "late_stop_tolerance_min": late_stop_tolerance_min,
        "no_evidence_grace_min": no_evidence_grace_min,
        "position_report_interval_seconds": position_report_interval_seconds,
    }
    for field, value in updates.items():
        if value is not None:
            setattr(row, field, value)
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row
