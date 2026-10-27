# ruff: noqa: N818 — NotAppealable / AlreadyReviewed name the condition the endpoint maps to a
# 409, same convention as app/domain/impediments/service.py.
"""Appeals service (spec 2026-10-20-alocacao-automatica §7.5): who may contest a released stop
and until when (`is_appealable`, the one predicate — the late check-in/out's `StopReleased`
reads it too), filing an appeal, and the manager's accept/reject.

Transaction discipline: these functions flush, never commit — the endpoint owns the transaction
boundary, same convention as app/domain/impediments/service.py."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import FieldWorker, Floor, ServicePoint
from app.domain.execution.models import Execution, ExecutionSource
from app.domain.identity.models import User
from app.domain.monitoring.alerts_service import alert_rule_enabled
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.monitoring.notifications import notify_appeal_filed
from app.domain.monitoring.push import PushSender
from app.domain.routing.models import ReleaseReason, Route, RouteStop, RouteStopStatus

# The releases the worker did not cause: an impediment is their own report, nothing to contest.
APPEALABLE_RELEASES = frozenset(
    {ReleaseReason.LEFT_CAMPUS, ReleaseReason.LOCATION_SILENCE, ReleaseReason.END_OF_DAY}
)

_REASON_PT_BR: dict[AppealReason, str] = {
    AppealReason.NO_SIGNAL: "fiquei sem sinal",
    AppealReason.LEFT_BRIEFLY: "saí rapidamente do campus",
    AppealReason.APP_CLOSED: "o app fechou/descarregou",
    AppealReason.OTHER: "outro motivo",
}


class NotAppealable(Exception):
    """The stop cannot be appealed by this worker now; `reason_code` says why: NOT_OWNER,
    NOT_RELEASED, ALREADY_CHECKED_OUT, REASON_NOT_APPEALABLE, ALREADY_APPEALED or
    WINDOW_EXPIRED."""

    def __init__(self, route_stop_id: uuid.UUID, reason_code: str) -> None:
        super().__init__(f'Route stop "{route_stop_id}" cannot be appealed ({reason_code}).')
        self.reason_code = reason_code


class AlreadyReviewed(Exception):
    """The appeal was already accepted or rejected; the decision is final."""


class AppealStopNotFound(Exception):
    """The appeal names a route stop that does not exist."""


def appeal_deadline(stop: RouteStop, *, window_hours: int) -> datetime:
    """The last instant the released `stop` may be appealed.

    deadline = appeal_deadline(stop, window_hours=settings.appeal_window_hours)
    """
    assert stop.released_at is not None  # release_stops stamps every RELEASED stop
    return stop.released_at + timedelta(hours=window_hours)


def is_appealable(stop: RouteStop, *, now: datetime, window_hours: int, has_appeal: bool) -> bool:
    """Whether the owner of `stop` may still appeal it (spec §7.5): released for a reason they
    did not cause, not appealed yet, and within `window_hours` of the release. Ownership is the
    caller's to check.

        appealable = is_appealable(stop, now=now, window_hours=48, has_appeal=False)
    """
    return _refusal_code(stop, now=now, window_hours=window_hours, has_appeal=has_appeal) is None


def _refusal_code(
    stop: RouteStop, *, now: datetime, window_hours: int, has_appeal: bool
) -> str | None:
    # A DONE stop's check-out reached the server: the work is on record, nothing to contest.
    if stop.status is RouteStopStatus.DONE:
        return "ALREADY_CHECKED_OUT"
    if stop.status is not RouteStopStatus.RELEASED:
        return "NOT_RELEASED"
    if stop.release_reason not in APPEALABLE_RELEASES:
        return "REASON_NOT_APPEALABLE"
    if has_appeal:
        return "ALREADY_APPEALED"
    if now > appeal_deadline(stop, window_hours=window_hours):
        return "WINDOW_EXPIRED"
    return None


async def stop_has_appeal(db: AsyncSession, route_stop_id: uuid.UUID) -> bool:
    """Whether the stop was already appealed (whatever the decision).

    if await stop_has_appeal(db, stop.id): ...
    """
    return bool(await db.scalar(select(exists().where(Appeal.route_stop_id == route_stop_id))))


async def appealable_stops(
    db: AsyncSession, *, worker: FieldWorker, now: datetime, window_hours: int
) -> list[RouteStop]:
    """The worker's stops they may still appeal, latest release first, with point, floor and
    building loaded — the app's "Pontos liberados que você pode contestar".

        stops = await appealable_stops(db, worker=worker, now=now, window_hours=48)
    """
    rows = await db.execute(
        select(RouteStop, Appeal.id)
        .join(Route, RouteStop.route_id == Route.id)
        .outerjoin(Appeal, Appeal.route_stop_id == RouteStop.id)
        .where(
            Route.field_worker_id == worker.id,
            RouteStop.status == RouteStopStatus.RELEASED,
            RouteStop.released_at >= now - timedelta(hours=window_hours),
        )
        .options(
            selectinload(RouteStop.service_point)
            .selectinload(ServicePoint.floor)
            .selectinload(Floor.building)
        )
        .order_by(RouteStop.released_at.desc())
    )
    return [
        stop
        for stop, appeal_id in rows.tuples()
        if is_appealable(stop, now=now, window_hours=window_hours, has_appeal=appeal_id is not None)
    ]


async def file_appeal(
    db: AsyncSession,
    *,
    worker: FieldWorker,
    route_stop_id: uuid.UUID,
    reason: AppealReason,
    note: str | None,
    claimed_executed_at: datetime | None,
    idempotency_key: uuid.UUID,
    now: datetime,
    window_hours: int,
    push: PushSender,
) -> Appeal:
    """The worker's appeal over a released stop, PENDING; the managers get the APPEAL_FILED
    alert and a notification. A replay of `idempotency_key` returns the first appeal untouched.

    Raises AppealStopNotFound for an unknown stop and NotAppealable when it is not this
    worker's to appeal now.

        appeal = await file_appeal(db, worker=worker, route_stop_id=stop.id,
                                   reason=AppealReason.NO_SIGNAL, note=None,
                                   claimed_executed_at=None, idempotency_key=key, now=now,
                                   window_hours=48, push=push)
    """
    stop = await _lock_stop(db, route_stop_id)
    replay = await db.scalar(select(Appeal).where(Appeal.idempotency_key == idempotency_key))
    if replay is not None:
        return replay
    await _ensure_appealable(db, stop=stop, worker=worker, now=now, window_hours=window_hours)
    appeal = Appeal(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        reason=reason,
        note=note,
        claimed_executed_at=claimed_executed_at,
        idempotency_key=idempotency_key,
    )
    db.add(appeal)
    await db.flush()
    await _announce(db, appeal=appeal, stop=stop, worker=worker, push=push)
    return appeal


async def _lock_stop(db: AsyncSession, route_stop_id: uuid.UUID) -> RouteStop:
    # IMPORTANT: the row lock serialises filings on one stop — a retry racing its original, or a
    # second appeal, waits here and then finds the first one instead of hitting the UNIQUE keys.
    stop = await db.scalar(
        select(RouteStop)
        .where(RouteStop.id == route_stop_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stop is None:
        raise AppealStopNotFound(f'Route stop "{route_stop_id}" not found.')
    return stop


async def _ensure_appealable(
    db: AsyncSession, *, stop: RouteStop, worker: FieldWorker, now: datetime, window_hours: int
) -> None:
    route = await db.get(Route, stop.route_id)
    if route is None or route.field_worker_id != worker.id:
        raise NotAppealable(stop.id, "NOT_OWNER")
    code = _refusal_code(
        stop, now=now, window_hours=window_hours, has_appeal=await stop_has_appeal(db, stop.id)
    )
    if code is not None:
        raise NotAppealable(stop.id, code)


async def _announce(
    db: AsyncSession, *, appeal: Appeal, stop: RouteStop, worker: FieldWorker, push: PushSender
) -> None:
    point = await db.get(ServicePoint, stop.service_point_id)
    point_name = point.name if point is not None else ""
    description = (
        f"{worker.full_name} recorreu da liberação do ponto {point_name}: "
        f"{_REASON_PT_BR[appeal.reason]}."
    )
    if await alert_rule_enabled(db, AlertKind.APPEAL_FILED):
        db.add(
            Alert(
                kind=AlertKind.APPEAL_FILED,
                severity=AlertSeverity.INFO,
                dedup_key=f"APPEAL_FILED:{appeal.id}",
                description=description,
                route_id=stop.route_id,
                route_stop_id=stop.id,
                field_worker_id=worker.id,
            )
        )
        await db.flush()
    await notify_appeal_filed(db, appeal_id=appeal.id, description=description, push=push)


# Everything the lists show: the worker, the reviewer, and the stop's point, floor and building.
_LIST_OPTIONS = (
    selectinload(Appeal.field_worker),
    selectinload(Appeal.reviewer),
    selectinload(Appeal.route_stop)
    .selectinload(RouteStop.service_point)
    .selectinload(ServicePoint.floor)
    .selectinload(Floor.building),
)


async def find_appeals(
    db: AsyncSession, *, status: AppealStatus | None, field_worker_id: uuid.UUID | None = None
) -> list[Appeal]:
    """Appeals (of `status`, of one worker), PENDING first — what the manager still has to
    decide — then newest first, with worker and point loaded.

        pending = await find_appeals(db, status=AppealStatus.PENDING)
    """
    stmt = (
        select(Appeal)
        .options(*_LIST_OPTIONS)
        .order_by((Appeal.status == AppealStatus.PENDING).desc(), Appeal.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(Appeal.status == status)
    if field_worker_id is not None:
        stmt = stmt.where(Appeal.field_worker_id == field_worker_id)
    return list(await db.scalars(stmt))


async def get_appeal(db: AsyncSession, appeal_id: uuid.UUID) -> Appeal | None:
    """One appeal loaded like a row of `find_appeals`, or None."""
    appeal: Appeal | None = await db.scalar(
        select(Appeal)
        .where(Appeal.id == appeal_id)
        .options(*_LIST_OPTIONS)
        .execution_options(populate_existing=True)
    )
    return appeal


async def accept_appeal(
    db: AsyncSession, *, appeal: Appeal, actor: User, note: str | None, now: datetime
) -> Appeal:
    """Accept the appeal: the stop gets an `APPEAL` execution by the appealing worker, stamped
    with the time they claimed (else the release time), no answers nor evidence (spec §7.5). The
    stop stays RELEASED — a point redone by somebody else keeps both executions. Audited.

    Raises AlreadyReviewed unless the appeal is PENDING.

        appeal = await accept_appeal(db, appeal=appeal, actor=manager, note=None, now=now)
    """
    await _lock_pending(db, appeal)
    stop = await db.get(RouteStop, appeal.route_stop_id)
    assert stop is not None
    assert stop.released_at is not None
    executed_at = appeal.claimed_executed_at or stop.released_at
    execution = Execution(
        route_stop_id=appeal.route_stop_id,
        field_worker_id=appeal.field_worker_id,
        checked_in_at=executed_at,
        checked_out_at=executed_at,
        synced_at=now,
        source=ExecutionSource.APPEAL,
        # The appeal's own id: accepting twice could never create a second execution.
        idempotency_key=appeal.id,
    )
    db.add(execution)
    await db.flush()
    await _decide(
        db,
        appeal,
        AppealStatus.ACCEPTED,
        actor=actor,
        note=note,
        now=now,
        audit_extra={"execution_id": str(execution.id)},
    )
    return appeal


async def reject_appeal(
    db: AsyncSession, *, appeal: Appeal, actor: User, note: str | None, now: datetime
) -> Appeal:
    """Reject the appeal: nothing changes besides the record (spec §7.5). Audited.

    Raises AlreadyReviewed unless the appeal is PENDING.

        appeal = await reject_appeal(db, appeal=appeal, actor=manager, note="...", now=now)
    """
    await _lock_pending(db, appeal)
    await _decide(db, appeal, AppealStatus.REJECTED, actor=actor, note=note, now=now)
    return appeal


async def _lock_pending(db: AsyncSession, appeal: Appeal) -> None:
    # IMPORTANT: re-read under a row lock — two managers deciding at once must not both see
    # PENDING (the second accept would insert a duplicate execution key).
    await db.refresh(appeal, with_for_update=True)
    if appeal.status is not AppealStatus.PENDING:
        raise AlreadyReviewed(f'Appeal "{appeal.id}" is {appeal.status.value}; expected PENDING.')


async def _decide(
    db: AsyncSession,
    appeal: Appeal,
    status: AppealStatus,
    *,
    actor: User,
    note: str | None,
    now: datetime,
    audit_extra: dict[str, str] | None = None,
) -> None:
    appeal.status = status
    appeal.reviewed_by = actor.id
    appeal.reviewed_at = now
    appeal.review_note = note
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="appeal",
        entity_id=appeal.id,
        action="accept" if status is AppealStatus.ACCEPTED else "reject",
        before={"status": AppealStatus.PENDING.value},
        after={"status": status.value, "note": note, **(audit_extra or {})},
    )
