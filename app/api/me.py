"""Etapa 11 (RNF10) — `GET /me/personal-data`: the calling user's own LGPD data export.

Any authenticated role may read *their own* aggregate. The read itself is auditable
personal-data access (RNF12), so the route records one `audit_trail` entry
(`entity_type="personal_data"`, `action="SELF_ACCESS"`) and commits it — the domain
builder stays a pure read.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.privacy.personal_data import build_personal_data

router = APIRouter(prefix="/me", tags=["me"])

_AnyUser = Annotated[
    User, Depends(require_role(UserRole.FIELD_WORKER, UserRole.MANAGER, UserRole.ADMIN))
]
_Db = Annotated[AsyncSession, Depends(get_db)]


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProfileOut(_Model):
    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime


class FieldWorkerOut(_Model):
    id: uuid.UUID
    full_name: str
    contractor_company_id: uuid.UUID
    service_type_ids: list[uuid.UUID]


class ConsentEntryOut(_Model):
    notice_version: int
    accepted_at: datetime


class AuthEventOut(_Model):
    event: str
    ip_address: str | None
    created_at: datetime


class TimeSpanOut(_Model):
    total: int
    first_at: datetime | None
    last_at: datetime | None


class EvidenceSummaryOut(_Model):
    total_items: int
    total_bytes: int


class NotificationsSummaryOut(_Model):
    total: int
    unread: int


class PersonalDataOut(_Model):
    profile: ProfileOut
    field_worker: FieldWorkerOut | None
    consent_history: list[ConsentEntryOut]
    auth_events: list[AuthEventOut]
    executions_summary: TimeSpanOut
    evidence_summary: EvidenceSummaryOut
    positions_summary: TimeSpanOut
    notifications_summary: NotificationsSummaryOut


@router.get("/personal-data", response_model=PersonalDataOut)
async def get_my_personal_data(user: _AnyUser, db: _Db) -> PersonalDataOut:
    data = await build_personal_data(db, user)
    await record_audit_trail(
        db,
        actor_id=user.id,
        entity_type="personal_data",
        entity_id=user.id,
        action="SELF_ACCESS",
        before=None,
        after=None,
    )
    await db.commit()
    return PersonalDataOut.model_validate(data)
