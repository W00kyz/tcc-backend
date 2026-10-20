"""Appeals (spec 2026-10-20-alocacao-automatica §5.4, §7.5) against the real testcontainer with a
frozen `now`: who may contest a released stop and until when, idempotent filing that alerts the
managers, and the manager's review — an accepted appeal becomes an `APPEAL` execution."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.core.security import hash_password
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.appeals.service import (
    AlreadyReviewed,
    NotAppealable,
    accept_appeal,
    appealable_stops,
    file_appeal,
    is_appealable,
    reject_appeal,
)
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import ContractorCompany, FieldWorker, ServicePoint
from app.domain.execution.models import Execution, ExecutionSource
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertRule, Notification
from app.domain.monitoring.push import LoggingPushSender
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

# Tuesday 10:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
_WINDOW_HOURS = 48
_RELEASED_AT = _NOW - timedelta(hours=1)


@dataclass
class _Released:
    company: ContractorCompany
    worker: FieldWorker
    route: Route
    stop: RouteStop
    point: ServicePoint


async def _manager(db: AsyncSession) -> User:
    manager = User(
        name="Larissa",
        email=f"{uuid.uuid4().hex[:8]}@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db.add(manager)
    await db.flush()
    return manager


async def _released(
    db: AsyncSession,
    *,
    reason: ReleaseReason = ReleaseReason.LEFT_CAMPUS,
    released_at: datetime = _RELEASED_AT,
) -> _Released:
    """A worker whose one stop of today went back to the pool for `reason` at `released_at`."""
    service_type = await make_service_type(db)
    (point,) = await make_points(db, service_type=service_type, names=["Sala 101"])
    assert service_type.contractor_company is not None
    worker = await make_worker(db, company=service_type.contractor_company)
    route = await make_route(db, worker=worker, day=_NOW.date(), points=[point])
    route.status = RouteStatus.PAUSED
    stop = route.stops[0]
    stop.status = RouteStopStatus.RELEASED
    stop.release_reason = reason
    stop.released_at = released_at
    await db.commit()
    return _Released(
        company=service_type.contractor_company, worker=worker, route=route, stop=stop, point=point
    )


async def _file(
    db: AsyncSession,
    released: _Released,
    *,
    worker: FieldWorker | None = None,
    idempotency_key: uuid.UUID | None = None,
    claimed_executed_at: datetime | None = None,
) -> Appeal:
    return await file_appeal(
        db,
        worker=worker or released.worker,
        route_stop_id=released.stop.id,
        reason=AppealReason.NO_SIGNAL,
        note="Fiquei sem sinal no subsolo.",
        claimed_executed_at=claimed_executed_at,
        idempotency_key=idempotency_key or uuid.uuid4(),
        now=_NOW,
        window_hours=_WINDOW_HOURS,
        push=LoggingPushSender(),
    )


async def _refusal(
    db: AsyncSession, released: _Released, *, worker: FieldWorker | None = None
) -> str:
    with pytest.raises(NotAppealable) as caught:
        await _file(db, released, worker=worker)
    return caught.value.reason_code


async def _count(db: AsyncSession, model: type[object]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# --- who may appeal, and until when ----------------------------------------------------------


async def test_left_campus_release_is_appealable_within_window(db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    released = await _released(db_session, reason=ReleaseReason.LEFT_CAMPUS)
    claimed = _RELEASED_AT - timedelta(minutes=20)

    eligible = await appealable_stops(
        db_session, worker=released.worker, now=_NOW, window_hours=_WINDOW_HOURS
    )
    appeal = await _file(db_session, released, claimed_executed_at=claimed)
    await db_session.commit()

    assert [stop.id for stop in eligible] == [released.stop.id]
    assert appeal.route_stop_id == released.stop.id
    assert appeal.field_worker_id == released.worker.id
    assert appeal.reason is AppealReason.NO_SIGNAL
    assert appeal.note == "Fiquei sem sinal no subsolo."
    assert appeal.claimed_executed_at == claimed
    assert appeal.status is AppealStatus.PENDING
    alert = await db_session.scalar(select(Alert))
    assert alert is not None
    assert alert.kind is AlertKind.APPEAL_FILED
    assert alert.dedup_key == f"APPEAL_FILED:{appeal.id}"
    assert alert.route_stop_id == released.stop.id
    assert alert.field_worker_id == released.worker.id
    assert alert.description == (
        "João da Silva recorreu da liberação do ponto Sala 101: fiquei sem sinal."
    )
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert notification.user_id == manager.id
    assert notification.kind == "APPEAL_FILED"
    assert notification.body == alert.description
    assert notification.data == {"appeal_id": str(appeal.id)}


async def test_end_of_day_release_is_appealable(db_session: AsyncSession) -> None:
    released = await _released(db_session, reason=ReleaseReason.END_OF_DAY)

    appeal = await _file(db_session, released)

    assert appeal.status is AppealStatus.PENDING


async def test_location_silence_release_is_appealable(db_session: AsyncSession) -> None:
    released = await _released(db_session, reason=ReleaseReason.LOCATION_SILENCE)

    appeal = await _file(db_session, released)

    assert appeal.status is AppealStatus.PENDING


@pytest.mark.parametrize("reason", [ReleaseReason.IMPEDIMENT, ReleaseReason.ROUTE_REMAINDER])
async def test_impediment_release_is_not(db_session: AsyncSession, reason: ReleaseReason) -> None:
    # The worker released it themselves: nothing to contest.
    released = await _released(db_session, reason=reason)

    eligible = await appealable_stops(
        db_session, worker=released.worker, now=_NOW, window_hours=_WINDOW_HOURS
    )

    assert eligible == []
    assert await _refusal(db_session, released) == "REASON_NOT_APPEALABLE"
    assert await _count(db_session, Appeal) == 0


async def test_checked_out_stop_is_not(db_session: AsyncSession) -> None:
    released = await _released(db_session)
    released.stop.status = RouteStopStatus.DONE
    released.stop.release_reason = None
    released.stop.released_at = None
    await db_session.commit()

    assert await _refusal(db_session, released) == "ALREADY_CHECKED_OUT"


async def test_live_stop_is_not(db_session: AsyncSession) -> None:
    released = await _released(db_session)
    released.stop.status = RouteStopStatus.PENDING
    released.stop.release_reason = None
    released.stop.released_at = None
    await db_session.commit()

    assert await _refusal(db_session, released) == "NOT_RELEASED"


async def test_window_expired(db_session: AsyncSession) -> None:
    released = await _released(
        db_session, released_at=_NOW - timedelta(hours=_WINDOW_HOURS, seconds=1)
    )

    eligible = await appealable_stops(
        db_session, worker=released.worker, now=_NOW, window_hours=_WINDOW_HOURS
    )

    assert eligible == []
    assert await _refusal(db_session, released) == "WINDOW_EXPIRED"


async def test_appeal_on_the_deadline_itself_is_accepted(db_session: AsyncSession) -> None:
    released = await _released(db_session, released_at=_NOW - timedelta(hours=_WINDOW_HOURS))

    appeal = await _file(db_session, released)

    assert appeal.status is AppealStatus.PENDING


async def test_other_workers_stop_is_not(db_session: AsyncSession) -> None:
    released = await _released(db_session)
    other = await make_worker(db_session, company=released.company)
    await db_session.commit()

    eligible = await appealable_stops(
        db_session, worker=other, now=_NOW, window_hours=_WINDOW_HOURS
    )

    assert eligible == []
    assert await _refusal(db_session, released, worker=other) == "NOT_OWNER"


async def test_idempotent_filing(db_session: AsyncSession) -> None:
    await _manager(db_session)
    released = await _released(db_session)
    key = uuid.uuid4()

    first = await _file(db_session, released, idempotency_key=key)
    await db_session.commit()
    replay = await _file(db_session, released, idempotency_key=key)
    await db_session.commit()

    assert replay.id == first.id
    assert await _count(db_session, Appeal) == 1
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 1


async def test_second_appeal_on_the_same_stop_is_refused(db_session: AsyncSession) -> None:
    released = await _released(db_session)
    await _file(db_session, released)
    await db_session.commit()

    eligible = await appealable_stops(
        db_session, worker=released.worker, now=_NOW, window_hours=_WINDOW_HOURS
    )

    assert eligible == []
    assert await _refusal(db_session, released) == "ALREADY_APPEALED"


async def test_is_appealable_is_the_one_predicate(db_session: AsyncSession) -> None:
    released = await _released(db_session)
    stop = released.stop

    assert is_appealable(stop, now=_NOW, window_hours=_WINDOW_HOURS, has_appeal=False)
    assert not is_appealable(stop, now=_NOW, window_hours=_WINDOW_HOURS, has_appeal=True)
    assert not is_appealable(stop, now=_NOW, window_hours=0, has_appeal=False)


async def test_disabled_rule_files_without_an_alert(db_session: AsyncSession) -> None:
    await _manager(db_session)
    await db_session.merge(AlertRule(rule_kind=AlertKind.APPEAL_FILED, enabled=False))
    released = await _released(db_session)

    await _file(db_session, released)

    assert await _count(db_session, Alert) == 0
    # The manager still receives the appeal (spec §8.4: Justificativas counts it).
    assert await _count(db_session, Notification) == 1


# --- the manager's review --------------------------------------------------------------------


async def test_accept_creates_appeal_execution(db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    released = await _released(db_session)
    claimed = _RELEASED_AT - timedelta(minutes=20)
    appeal = await _file(db_session, released, claimed_executed_at=claimed)
    await db_session.commit()
    reviewed_at = _NOW + timedelta(hours=2)

    appeal = await accept_appeal(
        db_session, appeal=appeal, actor=manager, note="Confirmado com a portaria.", now=reviewed_at
    )
    await db_session.commit()

    assert appeal.status is AppealStatus.ACCEPTED
    assert appeal.reviewed_by == manager.id
    assert appeal.reviewed_at == reviewed_at
    assert appeal.review_note == "Confirmado com a portaria."
    execution = await db_session.scalar(select(Execution))
    assert execution is not None
    assert execution.source is ExecutionSource.APPEAL
    assert execution.route_stop_id == released.stop.id
    assert execution.field_worker_id == released.worker.id
    assert execution.checked_in_at == claimed
    assert execution.checked_out_at == claimed
    assert execution.synced_at == reviewed_at
    assert execution.idempotency_key == appeal.id
    await db_session.refresh(released.stop)
    assert released.stop.status is RouteStopStatus.RELEASED
    audit = await db_session.scalar(select(AuditTrail).where(AuditTrail.entity_type == "appeal"))
    assert audit is not None
    assert (audit.actor_id, audit.entity_id, audit.action) == (manager.id, appeal.id, "accept")
    assert audit.before_data == {"status": "PENDING"}
    assert audit.after_data == {
        "status": "ACCEPTED",
        "note": "Confirmado com a portaria.",
        "execution_id": str(execution.id),
    }


async def test_accept_without_a_claimed_time_uses_the_release_time(
    db_session: AsyncSession,
) -> None:
    manager = await _manager(db_session)
    released = await _released(db_session)
    appeal = await _file(db_session, released)

    await accept_appeal(db_session, appeal=appeal, actor=manager, note=None, now=_NOW)

    execution = await db_session.scalar(select(Execution))
    assert execution is not None
    assert execution.checked_in_at == _RELEASED_AT
    assert execution.checked_out_at == _RELEASED_AT


async def test_accept_when_point_redone_by_other_keeps_both(db_session: AsyncSession) -> None:
    # The released point went to another worker, who did it; the appeal still counts — the
    # point counts once in the reports (B6), both executions stay.
    manager = await _manager(db_session)
    released = await _released(db_session)
    appeal = await _file(db_session, released)
    other = await make_worker(db_session, company=released.company)
    other_route = await make_route(
        db_session, worker=other, day=_NOW.date(), points=[released.point]
    )
    other_stop = other_route.stops[0]
    other_stop.status = RouteStopStatus.DONE
    db_session.add(
        Execution(
            route_stop_id=other_stop.id,
            field_worker_id=other.id,
            checked_in_at=_NOW,
            checked_out_at=_NOW,
            synced_at=_NOW,
            source=ExecutionSource.APP,
            idempotency_key=uuid.uuid4(),
        )
    )
    await db_session.commit()

    await accept_appeal(db_session, appeal=appeal, actor=manager, note=None, now=_NOW)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Execution.field_worker_id, Execution.source).order_by(Execution.source)
        )
    ).all()
    assert sorted((worker_id, source.value) for worker_id, source in rows) == sorted(
        [(other.id, "APP"), (released.worker.id, "APPEAL")]
    )


async def test_reject_changes_nothing_else(db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    released = await _released(db_session)
    appeal = await _file(db_session, released)
    await db_session.commit()

    appeal = await reject_appeal(
        db_session, appeal=appeal, actor=manager, note="Sem registro na portaria.", now=_NOW
    )
    await db_session.commit()

    assert appeal.status is AppealStatus.REJECTED
    assert appeal.reviewed_by == manager.id
    assert appeal.reviewed_at == _NOW
    assert appeal.review_note == "Sem registro na portaria."
    assert await _count(db_session, Execution) == 0
    await db_session.refresh(released.stop)
    assert released.stop.status is RouteStopStatus.RELEASED
    audit = await db_session.scalar(select(AuditTrail).where(AuditTrail.entity_type == "appeal"))
    assert audit is not None
    assert audit.action == "reject"
    assert audit.after_data == {"status": "REJECTED", "note": "Sem registro na portaria."}


async def test_double_review_conflict(db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    released = await _released(db_session)
    appeal = await _file(db_session, released)
    await accept_appeal(db_session, appeal=appeal, actor=manager, note=None, now=_NOW)
    await db_session.commit()

    with pytest.raises(AlreadyReviewed):
        await accept_appeal(db_session, appeal=appeal, actor=manager, note=None, now=_NOW)
    with pytest.raises(AlreadyReviewed):
        await reject_appeal(db_session, appeal=appeal, actor=manager, note=None, now=_NOW)
    assert await _count(db_session, Execution) == 1
