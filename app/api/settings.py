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
    updated_at: datetime | None


class SettingsUpdate(BaseModel):
    check_radius_meters: int = Field(ge=1, le=1000)


@router.get("", response_model=SettingsOut)
async def get_settings_endpoint(_actor: _Reader, db: _Db) -> SettingsOut:
    row = await get_settings_row(db)
    return SettingsOut(check_radius_meters=row.check_radius_meters, updated_at=row.updated_at)


@router.put("", response_model=SettingsOut)
async def put_settings_endpoint(body: SettingsUpdate, actor: _Admin, db: _Db) -> SettingsOut:
    old = (await get_settings_row(db)).check_radius_meters
    row = await update_settings(db, actor_id=actor.id, check_radius_meters=body.check_radius_meters)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="system_settings",
        entity_id=_SETTINGS_ENTITY_ID,
        action="update_settings",
        before={"check_radius_meters": old},
        after={"check_radius_meters": row.check_radius_meters},
    )
    await db.commit()
    return SettingsOut(check_radius_meters=row.check_radius_meters, updated_at=row.updated_at)
