"""Field worker review for managers/admins. Workers register themselves (POST /auth/register);
here a manager lists them, approves or rejects a registration and switches an approved worker
off or back on. There is no hard delete: workers are referenced by routes and check-ins."""

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CampusToday, require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStop, RouteStopStatus

router = APIRouter(prefix="/field-workers", tags=["field-workers"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

_WITH_PEOPLE = (selectinload(FieldWorker.user), selectinload(FieldWorker.contractor_company))


class FieldWorkerOut(BaseModel):
    id: UUID
    full_name: str
    email: str
    contractor_company_id: UUID
    contractor_company_name: str
    status: FieldWorkerStatus
    is_active: bool
    rejection_reason: str | None
    created_at: datetime
    done_today: int


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


async def _done_today(db: AsyncSession, today: date) -> dict[UUID, int]:
    """Stops each worker finished on `today`, by worker id (workers with none are absent)."""
    rows = await db.execute(
        select(Route.field_worker_id, func.count())
        .select_from(RouteStop)
        .join(Route, RouteStop.route_id == Route.id)
        .where(RouteStop.service_date == today, RouteStop.status == RouteStopStatus.DONE)
        .group_by(Route.field_worker_id)
    )
    return dict(rows.tuples().all())


def _to_out(worker: FieldWorker, done_today: int) -> FieldWorkerOut:
    return FieldWorkerOut(
        id=worker.id,
        full_name=worker.full_name,
        email=worker.user.email,
        contractor_company_id=worker.contractor_company_id,
        contractor_company_name=worker.contractor_company.name,
        status=worker.status,
        is_active=worker.is_active,
        rejection_reason=worker.rejection_reason,
        created_at=worker.created_at,
        done_today=done_today,
    )


async def _load(db: AsyncSession, worker_id: UUID) -> FieldWorker:
    worker = await db.scalar(
        select(FieldWorker).where(FieldWorker.id == worker_id).options(*_WITH_PEOPLE)
    )
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Field worker "{worker_id}" not found.')
    return worker


def _require_status(worker: FieldWorker, action: str, *allowed: FieldWorkerStatus) -> None:
    if worker.status not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_WORKER_STATUS",
                "detail": f"Cannot {action} a field worker whose status is {worker.status.value}.",
            },
        )


def _snapshot(worker: FieldWorker) -> dict[str, object]:
    return {
        "status": worker.status.value,
        "is_active": worker.is_active,
        "rejection_reason": worker.rejection_reason,
    }


async def _audited_change(
    db: AsyncSession,
    actor: User,
    worker: FieldWorker,
    action: str,
    today: date,
    *,
    status_: FieldWorkerStatus | None = None,
    is_active: bool | None = None,
    rejection_reason: str | None = None,
) -> FieldWorkerOut:
    """Apply a review decision (or an on/off switch), audit it and answer with the new state."""
    before = _snapshot(worker)
    if status_ is not None:
        worker.status = status_
        worker.reviewed_by = actor.id
        worker.reviewed_at = datetime.now(UTC)
        worker.rejection_reason = rejection_reason
    if is_active is not None:
        worker.is_active = is_active
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="field_worker",
        entity_id=worker.id,
        action=action,
        before=before,
        after=_snapshot(worker),
    )
    await db.commit()
    return _to_out(worker, (await _done_today(db, today)).get(worker.id, 0))


@router.get("", response_model=list[FieldWorkerOut])
async def list_field_workers(_actor: _Manager, db: _Db, today: CampusToday) -> list[FieldWorkerOut]:
    workers = (
        await db.scalars(
            select(FieldWorker).options(*_WITH_PEOPLE).order_by(FieldWorker.created_at)
        )
    ).all()
    done = await _done_today(db, today)
    return [_to_out(worker, done.get(worker.id, 0)) for worker in workers]


@router.post("/{worker_id}/approve", response_model=FieldWorkerOut)
async def approve_field_worker(
    worker_id: UUID, actor: _Manager, db: _Db, today: CampusToday
) -> FieldWorkerOut:
    worker = await _load(db, worker_id)
    _require_status(
        worker, "approve", FieldWorkerStatus.PENDING_APPROVAL, FieldWorkerStatus.REJECTED
    )
    return await _audited_change(
        db, actor, worker, "approve", today, status_=FieldWorkerStatus.APPROVED
    )


@router.post("/{worker_id}/reject", response_model=FieldWorkerOut)
async def reject_field_worker(
    worker_id: UUID, body: RejectRequest, actor: _Manager, db: _Db, today: CampusToday
) -> FieldWorkerOut:
    worker = await _load(db, worker_id)
    _require_status(worker, "reject", FieldWorkerStatus.PENDING_APPROVAL)
    return await _audited_change(
        db,
        actor,
        worker,
        "reject",
        today,
        status_=FieldWorkerStatus.REJECTED,
        rejection_reason=body.reason,
    )


@router.post("/{worker_id}/deactivate", response_model=FieldWorkerOut)
async def deactivate_field_worker(
    worker_id: UUID, actor: _Manager, db: _Db, today: CampusToday
) -> FieldWorkerOut:
    worker = await _load(db, worker_id)
    _require_status(worker, "deactivate", FieldWorkerStatus.APPROVED)
    return await _audited_change(db, actor, worker, "deactivate", today, is_active=False)


@router.post("/{worker_id}/reactivate", response_model=FieldWorkerOut)
async def reactivate_field_worker(
    worker_id: UUID, actor: _Manager, db: _Db, today: CampusToday
) -> FieldWorkerOut:
    worker = await _load(db, worker_id)
    _require_status(worker, "reactivate", FieldWorkerStatus.APPROVED)
    return await _audited_change(db, actor, worker, "reactivate", today, is_active=True)
