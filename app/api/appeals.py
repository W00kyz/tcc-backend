"""The appeal surface (spec 2026-10-20-alocacao-automatica §7.5, §8.4): a field worker lists the
released points they may still contest and files an appeal; a manager lists the appeals and
accepts (the stop gets an `APPEAL` execution) or rejects them.

Every endpoint owns its transaction boundary — call the flush-only service, then `db.commit()` —
the same discipline as `app/api/impediments.py`."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApprovedWorker, Now, require_role
from app.db.session import get_db
from app.domain.appeals.models import APPEAL_NOTE_MAX_LENGTH, Appeal, AppealReason, AppealStatus
from app.domain.appeals.service import (
    AlreadyReviewed,
    AppealStopNotFound,
    NotAppealable,
    accept_appeal,
    appeal_deadline,
    appealable_stops,
    file_appeal,
    find_appeals,
    get_appeal,
    reject_appeal,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import ReleaseReason, RouteStop
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/appeals", tags=["appeals"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]


class AppealableStopOut(BaseModel):
    route_stop_id: UUID
    service_point_id: UUID
    service_point_name: str
    building_name: str
    floor_label: str
    released_at: datetime
    release_reason: ReleaseReason
    # The last instant an appeal is accepted (`released_at` + `appeal_window_hours`).
    deadline: datetime


class AppealBody(BaseModel):
    route_stop_id: UUID
    reason: AppealReason
    # Bounded like the `appeals.note` column — an over-long note would otherwise be a 500.
    note: str | None = Field(default=None, max_length=APPEAL_NOTE_MAX_LENGTH)
    # The device-local check-in time, when the app has one.
    claimed_executed_at: datetime | None = None
    idempotency_key: UUID


class AppealReviewBody(BaseModel):
    note: str | None = Field(default=None, max_length=APPEAL_NOTE_MAX_LENGTH)


class AppealOut(BaseModel):
    id: UUID
    route_stop_id: UUID
    field_worker_id: UUID
    field_worker_name: str
    service_point_id: UUID
    service_point_name: str
    building_name: str
    released_at: datetime
    release_reason: ReleaseReason
    reason: AppealReason
    note: str | None
    claimed_executed_at: datetime | None
    status: AppealStatus
    filed_at: datetime
    reviewed_at: datetime | None
    review_note: str | None


def _stop_out(stop: RouteStop, *, window_hours: int) -> AppealableStopOut:
    """Expects the point, floor and building `appealable_stops` loads."""
    assert stop.released_at is not None
    assert stop.release_reason is not None
    point = stop.service_point
    return AppealableStopOut(
        route_stop_id=stop.id,
        service_point_id=point.id,
        service_point_name=point.name,
        building_name=point.floor.building.name,
        floor_label=point.floor.label,
        released_at=stop.released_at,
        release_reason=stop.release_reason,
        deadline=appeal_deadline(stop, window_hours=window_hours),
    )


def _to_out(appeal: Appeal) -> AppealOut:
    """Expects the relationships `find_appeals`/`get_appeal` load."""
    stop = appeal.route_stop
    assert stop.released_at is not None
    assert stop.release_reason is not None
    point = stop.service_point
    return AppealOut(
        id=appeal.id,
        route_stop_id=appeal.route_stop_id,
        field_worker_id=appeal.field_worker_id,
        field_worker_name=appeal.field_worker.full_name,
        service_point_id=point.id,
        service_point_name=point.name,
        building_name=point.floor.building.name,
        released_at=stop.released_at,
        release_reason=stop.release_reason,
        reason=appeal.reason,
        note=appeal.note,
        claimed_executed_at=appeal.claimed_executed_at,
        status=appeal.status,
        filed_at=appeal.created_at,
        reviewed_at=appeal.reviewed_at,
        review_note=appeal.review_note,
    )


async def _load_appeal(db: AsyncSession, appeal_id: UUID) -> Appeal:
    appeal = await get_appeal(db, appeal_id)
    if appeal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Appeal "{appeal_id}" not found.')
    return appeal


def _already_reviewed(exc: AlreadyReviewed) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, detail={"code": "ALREADY_REVIEWED", "detail": str(exc)}
    )


# --- the field worker ------------------------------------------------------------------------


@router.get("/eligible", response_model=list[AppealableStopOut])
async def list_appealable_stops(
    worker: ApprovedWorker, db: _Db, now: Now
) -> list[AppealableStopOut]:
    """The app's "Pontos liberados que você pode contestar": the worker's released stops they
    may still appeal, latest release first."""
    window_hours = (await get_settings_row(db)).appeal_window_hours
    stops = await appealable_stops(db, worker=worker, now=now, window_hours=window_hours)
    return [_stop_out(stop, window_hours=window_hours) for stop in stops]


@router.post("", response_model=AppealOut, status_code=status.HTTP_201_CREATED)
async def create_appeal(
    body: AppealBody, request: Request, worker: ApprovedWorker, db: _Db, now: Now
) -> AppealOut:
    """The worker declares they executed a released stop (spec §7.5). Idempotent on
    `idempotency_key`; 409 `NOT_APPEALABLE` with a `reason_code` when it is not theirs to
    appeal now."""
    try:
        appeal = await file_appeal(
            db,
            worker=worker,
            route_stop_id=body.route_stop_id,
            reason=body.reason,
            note=body.note,
            claimed_executed_at=body.claimed_executed_at,
            idempotency_key=body.idempotency_key,
            now=now,
            window_hours=(await get_settings_row(db)).appeal_window_hours,
            push=request.app.state.push_sender,
        )
    except AppealStopNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except NotAppealable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "NOT_APPEALABLE", "detail": str(exc), "reason_code": exc.reason_code},
        ) from exc
    await db.commit()
    return _to_out(await _load_appeal(db, appeal.id))


@router.get("/mine", response_model=list[AppealOut])
async def list_my_appeals(worker: ApprovedWorker, db: _Db) -> list[AppealOut]:
    """The worker's own appeals and their decisions, pending first."""
    return [_to_out(a) for a in await find_appeals(db, status=None, field_worker_id=worker.id)]


# --- the manager -----------------------------------------------------------------------------


@router.get("", response_model=list[AppealOut])
async def list_appeals(
    _actor: _Manager,
    db: _Db,
    appeal_status: Annotated[AppealStatus | None, Query(alias="status")] = None,
) -> list[AppealOut]:
    """The manager's Justificativas "Recursos" tab (spec §8.4): every appeal, PENDING first, or
    only those of `?status=`."""
    return [_to_out(a) for a in await find_appeals(db, status=appeal_status)]


@router.post("/{appeal_id}/accept", response_model=AppealOut)
async def accept(
    appeal_id: UUID, body: AppealReviewBody, actor: _Manager, db: _Db, now: Now
) -> AppealOut:
    """Accept: the stop gets an `APPEAL` execution by the worker. 409 `ALREADY_REVIEWED` once
    decided."""
    appeal = await _load_appeal(db, appeal_id)
    try:
        await accept_appeal(db, appeal=appeal, actor=actor, note=body.note, now=now)
    except AlreadyReviewed as exc:
        raise _already_reviewed(exc) from exc
    await db.commit()
    return _to_out(await _load_appeal(db, appeal_id))


@router.post("/{appeal_id}/reject", response_model=AppealOut)
async def reject(
    appeal_id: UUID, body: AppealReviewBody, actor: _Manager, db: _Db, now: Now
) -> AppealOut:
    """Reject: nothing changes besides the record. 409 `ALREADY_REVIEWED` once decided."""
    appeal = await _load_appeal(db, appeal_id)
    try:
        await reject_appeal(db, appeal=appeal, actor=actor, note=body.note, now=now)
    except AlreadyReviewed as exc:
        raise _already_reviewed(exc) from exc
    await db.commit()
    return _to_out(await _load_appeal(db, appeal_id))
