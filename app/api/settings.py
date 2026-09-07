"""RF32 — the configurable tolerance radius used by check-in / check-out geo-validation.

`GET /settings` is readable by managers and admins; `PUT /settings` is admin-only and writes
an audit-trail entry. `update_settings` flushes; this router owns `db.commit()`."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
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
    late_start_tolerance_min: int
    absence_tolerance_min: int
    late_stop_tolerance_min: int
    no_evidence_grace_min: int
    position_report_interval_seconds: int
    updated_at: datetime | None


# Intervals must be at least a second; tolerances/grace periods may be zero (no slack).
class SettingsUpdate(BaseModel):
    check_radius_meters: int | None = Field(default=None, ge=1, le=1000)
    alert_scan_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    late_start_tolerance_min: int | None = Field(default=None, ge=0, le=1440)
    absence_tolerance_min: int | None = Field(default=None, ge=0, le=1440)
    late_stop_tolerance_min: int | None = Field(default=None, ge=0, le=1440)
    no_evidence_grace_min: int | None = Field(default=None, ge=0, le=1440)
    position_report_interval_seconds: int | None = Field(default=None, ge=1, le=3600)


_SETTINGS_FIELDS = (
    "check_radius_meters",
    "alert_scan_interval_seconds",
    "late_start_tolerance_min",
    "absence_tolerance_min",
    "late_stop_tolerance_min",
    "no_evidence_grace_min",
    "position_report_interval_seconds",
)


def _to_settings_out(row: SystemSettings) -> SettingsOut:
    return SettingsOut(
        check_radius_meters=row.check_radius_meters,
        alert_scan_interval_seconds=row.alert_scan_interval_seconds,
        late_start_tolerance_min=row.late_start_tolerance_min,
        absence_tolerance_min=row.absence_tolerance_min,
        late_stop_tolerance_min=row.late_stop_tolerance_min,
        no_evidence_grace_min=row.no_evidence_grace_min,
        position_report_interval_seconds=row.position_report_interval_seconds,
        updated_at=row.updated_at,
    )


def _settings_snapshot(row: SystemSettings) -> dict[str, int]:
    return {field: getattr(row, field) for field in _SETTINGS_FIELDS}


@router.get("", response_model=SettingsOut)
async def get_settings_endpoint(_actor: _Reader, db: _Db) -> SettingsOut:
    return _to_settings_out(await get_settings_row(db))


@router.put("", response_model=SettingsOut)
async def put_settings_endpoint(body: SettingsUpdate, actor: _Admin, db: _Db) -> SettingsOut:
    before = _settings_snapshot(await get_settings_row(db))
    row = await update_settings(
        db,
        actor_id=actor.id,
        check_radius_meters=body.check_radius_meters,
        alert_scan_interval_seconds=body.alert_scan_interval_seconds,
        late_start_tolerance_min=body.late_start_tolerance_min,
        absence_tolerance_min=body.absence_tolerance_min,
        late_stop_tolerance_min=body.late_stop_tolerance_min,
        no_evidence_grace_min=body.no_evidence_grace_min,
        position_report_interval_seconds=body.position_report_interval_seconds,
    )
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
