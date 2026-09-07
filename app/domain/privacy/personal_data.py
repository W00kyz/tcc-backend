"""LGPD (RNF10) — "consulta aos seus dados": the aggregate of everything tied to one
login identity. A pure read: `build_personal_data(db, user)` runs one aggregate query per
summary (never a row-by-row dump) and returns a plain dataclass. The route in
`app/api/me.py` owns the audit-trail write (RNF12) and the commit — this stays free of
side effects so it composes and tests in isolation.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from app.domain.catalog.models import FieldWorker
from app.domain.execution.models import EvidenceItem, Execution
from app.domain.identity.models import AuthLog, User
from app.domain.monitoring.models import Notification, WorkerPosition
from app.domain.privacy.models import ConsentRecord, PrivacyNotice

_AUTH_EVENTS_LIMIT = 20


@dataclass(frozen=True)
class ProfileData:
    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class FieldWorkerData:
    id: uuid.UUID
    full_name: str
    contractor_company_id: uuid.UUID
    service_type_ids: list[uuid.UUID]


@dataclass(frozen=True)
class ConsentEntry:
    notice_version: int
    accepted_at: datetime


@dataclass(frozen=True)
class AuthEventEntry:
    event: str
    ip_address: str | None
    created_at: datetime


@dataclass(frozen=True)
class TimeSpanSummary:
    total: int
    first_at: datetime | None
    last_at: datetime | None


@dataclass(frozen=True)
class EvidenceSummary:
    total_items: int
    total_bytes: int


@dataclass(frozen=True)
class NotificationsSummary:
    total: int
    unread: int


@dataclass(frozen=True)
class PersonalData:
    profile: ProfileData
    field_worker: FieldWorkerData | None
    consent_history: list[ConsentEntry]
    auth_events: list[AuthEventEntry]
    executions_summary: TimeSpanSummary
    evidence_summary: EvidenceSummary
    positions_summary: TimeSpanSummary
    notifications_summary: NotificationsSummary


async def build_personal_data(db: AsyncSession, user: User) -> PersonalData:
    """Everything the calling user's identity is tied to, as one snapshot.

    data = await build_personal_data(db, current_user)
    """
    worker = await db.scalar(
        select(FieldWorker)
        .where(FieldWorker.user_id == user.id)
        .options(selectinload(FieldWorker.service_type_links))
    )

    return PersonalData(
        profile=ProfileData(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role.value,
            is_active=user.is_active,
            created_at=user.created_at,
        ),
        field_worker=_field_worker_data(worker),
        consent_history=await _consent_history(db, user.id),
        auth_events=await _auth_events(db, user.id),
        executions_summary=await _time_span(
            db, Execution.checked_in_at, Execution.field_worker_id, worker
        ),
        evidence_summary=await _evidence_summary(db, worker),
        positions_summary=await _time_span(
            db, WorkerPosition.received_at, WorkerPosition.field_worker_id, worker
        ),
        notifications_summary=await _notifications_summary(db, user.id),
    )


def _field_worker_data(worker: FieldWorker | None) -> FieldWorkerData | None:
    if worker is None:
        return None
    return FieldWorkerData(
        id=worker.id,
        full_name=worker.full_name,
        contractor_company_id=worker.contractor_company_id,
        service_type_ids=[link.service_type_id for link in worker.service_type_links],
    )


async def _consent_history(db: AsyncSession, user_id: uuid.UUID) -> list[ConsentEntry]:
    rows = await db.execute(
        select(PrivacyNotice.version, ConsentRecord.accepted_at)
        .join(PrivacyNotice, ConsentRecord.privacy_notice_id == PrivacyNotice.id)
        .where(ConsentRecord.user_id == user_id)
        .order_by(PrivacyNotice.version)
    )
    return [
        ConsentEntry(notice_version=version, accepted_at=accepted_at)
        for version, accepted_at in rows
    ]


async def _auth_events(db: AsyncSession, user_id: uuid.UUID) -> list[AuthEventEntry]:
    rows = await db.execute(
        select(AuthLog.event, AuthLog.ip_address, AuthLog.created_at)
        .where(AuthLog.user_id == user_id)
        .order_by(AuthLog.created_at.desc())
        .limit(_AUTH_EVENTS_LIMIT)
    )
    return [
        AuthEventEntry(
            event=event.value,
            # asyncpg returns an ipaddress object for INET; the wire shape is a string.
            ip_address=None if ip_address is None else str(ip_address),
            created_at=created_at,
        )
        for event, ip_address, created_at in rows
    ]


async def _time_span(
    db: AsyncSession,
    when_column: InstrumentedAttribute[datetime],
    worker_column: InstrumentedAttribute[uuid.UUID],
    worker: FieldWorker | None,
) -> TimeSpanSummary:
    """Count + earliest + latest of a worker-scoped table in a single aggregate query.
    A caller with no field worker profile owns no such rows — answer an empty span."""
    if worker is None:
        return TimeSpanSummary(total=0, first_at=None, last_at=None)
    total, first_at, last_at = (
        await db.execute(
            select(func.count(), func.min(when_column), func.max(when_column)).where(
                worker_column == worker.id
            )
        )
    ).one()
    return TimeSpanSummary(total=total, first_at=first_at, last_at=last_at)


async def _evidence_summary(db: AsyncSession, worker: FieldWorker | None) -> EvidenceSummary:
    if worker is None:
        return EvidenceSummary(total_items=0, total_bytes=0)
    total_items, total_bytes = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(EvidenceItem.byte_size), 0))
            .join(Execution, EvidenceItem.execution_id == Execution.id)
            .where(Execution.field_worker_id == worker.id)
        )
    ).one()
    return EvidenceSummary(total_items=total_items, total_bytes=total_bytes)


async def _notifications_summary(db: AsyncSession, user_id: uuid.UUID) -> NotificationsSummary:
    total, unread = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Notification.read_at.is_(None)),
            ).where(Notification.user_id == user_id)
        )
    ).one()
    return NotificationsSummary(total=total, unread=unread)
