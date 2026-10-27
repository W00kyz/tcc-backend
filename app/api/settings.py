"""RF32 — the system settings: the check-in tolerance radius, the alert-scan defaults and the
automatic-allocation parameters (spec 2026-10-20-alocacao-automatica §5.5).

`GET /settings` is readable by managers and admins; `PUT /settings` is admin-only and writes
an audit-trail entry. `update_settings` flushes; this router owns `db.commit()`."""

from datetime import datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row, update_settings

router = APIRouter(prefix="/settings", tags=["settings"])

_Reader = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# The settings row has a boolean singleton PK, so it has no UUID of its own; the audit trail's
# entity_id column is a non-null UUID. Use a fixed sentinel to identify "the settings row".
_SETTINGS_ENTITY_ID = UUID(int=0)


class SettingsOut(BaseModel):
    check_radius_meters: int
    # Etapa 9 (spec §2 Ruling 8): the periodic alert scan's global tolerances/cadence.
    alert_scan_interval_seconds: int
    no_evidence_grace_min: int
    position_report_interval_seconds: int
    # Automatic allocation and release (spec 2026-10-20-alocacao-automatica §5.5).
    allocation_batch_size: int
    campus_min_lat: float
    campus_max_lat: float
    campus_min_lng: float
    campus_max_lng: float
    outside_campus_release_minutes: int
    location_silence_warning_minutes: int
    location_silence_release_minutes: int
    workday_end_time: time  # campus local time
    service_weekdays: list[int]  # Monday=0 ... Sunday=6
    appeal_window_hours: int
    updated_at: datetime | None


_Latitude = Annotated[float, Field(ge=-90, le=90)]
_Longitude = Annotated[float, Field(ge=-180, le=180)]
_Minutes = Annotated[int, Field(ge=1, le=1440)]
_Weekday = Annotated[int, Field(ge=0, le=6)]


# Intervals must be at least a second; tolerances/grace periods may be zero (no slack).
class SettingsUpdate(BaseModel):
    check_radius_meters: int | None = Field(default=None, ge=1, le=1000)
    alert_scan_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    no_evidence_grace_min: int | None = Field(default=None, ge=0, le=1440)
    position_report_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    allocation_batch_size: int | None = Field(default=None, ge=1, le=50)
    campus_min_lat: _Latitude | None = None
    campus_max_lat: _Latitude | None = None
    campus_min_lng: _Longitude | None = None
    campus_max_lng: _Longitude | None = None
    outside_campus_release_minutes: _Minutes | None = None
    location_silence_warning_minutes: _Minutes | None = None
    location_silence_release_minutes: _Minutes | None = None
    workday_end_time: time | None = None
    service_weekdays: list[_Weekday] | None = Field(default=None, min_length=1, max_length=7)
    appeal_window_hours: int | None = Field(default=None, ge=1, le=720)

    @field_validator("service_weekdays")
    @classmethod
    def _sorted_unique_weekdays(cls, value: list[int] | None) -> list[int] | None:
        return sorted(set(value)) if value is not None else None


def _to_settings_out(row: SystemSettings) -> SettingsOut:
    return SettingsOut.model_validate(row, from_attributes=True)


def _settings_snapshot(row: SystemSettings) -> dict[str, object]:
    # JSON mode: the audit trail is JSONB, and `workday_end_time` is a `time`.
    return _to_settings_out(row).model_dump(mode="json", exclude={"updated_at"})


@router.get("", response_model=SettingsOut)
async def get_settings_endpoint(_actor: _Reader, db: _Db) -> SettingsOut:
    return _to_settings_out(await get_settings_row(db))


@router.put("", response_model=SettingsOut)
async def put_settings_endpoint(body: SettingsUpdate, actor: _Admin, db: _Db) -> SettingsOut:
    before = _settings_snapshot(await get_settings_row(db))
    row = await update_settings(db, actor_id=actor.id, changes=body.model_dump(exclude_none=True))
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="system_settings",
        entity_id=_SETTINGS_ENTITY_ID,
        action="update_settings",
        before=before,
        after=_settings_snapshot(row),
    )
    await db.commit()
    return _to_settings_out(row)
