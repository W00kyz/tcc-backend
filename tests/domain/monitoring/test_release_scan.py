"""The release scan (spec 2026-10-20-alocacao-automatica §7.2-§7.4) against the real
testcontainer with a frozen `now`: a worker outside the campus or silent for too long has their
batch given back to the pool, and the end of the workday closes every open route and raises one
"pontos não atendidos" alert for the day."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.core.campus_clock import campus_today
from app.core.security import hash_password
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import FieldWorker, ServicePoint, ServiceType
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertSeverity,
    Notification,
    WorkerPosition,
)
from app.domain.monitoring.release_scan import run_release_scan
from app.domain.routing.allocation import free_points
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import (
    make_check_in,
    make_points,
    make_route,
    make_service_type,
    make_worker,
)

# Tuesday 10:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
_TODAY = campus_today(_NOW)
# Tuesday 17:00 on campus — the default end of the workday.
_FIVE_PM = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
_SATURDAY = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)
_INSIDE = (-7.2150, -35.9100)
_OUTSIDE = (-7.2300, -35.9100)


@dataclass
class _Working:
    service_type: ServiceType
    worker: FieldWorker
    route: Route
    points: list[ServicePoint]


async def _manager(db: AsyncSession) -> User:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db.add(manager)
    await db.flush()
    return manager


async def _working(
    db: AsyncSession,
    *,
    allocated_minutes_ago: int = 30,
    now: datetime = _NOW,
    rooms: int = 2,
    service_type: ServiceType | None = None,
) -> _Working:
    """A worker of a typed company (a new "Limpeza" one unless `service_type` is given) on their
    route of `now`'s day, one batch of `rooms` PENDING stops handed out `allocated_minutes_ago`."""
    service_type = service_type or await make_service_type(db)
    points = await make_points(
        db, service_type=service_type, names=[f"Sala {i}" for i in range(rooms)]
    )
    assert service_type.contractor_company is not None
    worker = await make_worker(db, company=service_type.contractor_company)
    route = await make_route(db, worker=worker, day=campus_today(now), points=points)
    route.started_at = now - timedelta(minutes=allocated_minutes_ago)
    route.last_allocated_at = now - timedelta(minutes=allocated_minutes_ago)
    await db.commit()
    return _Working(service_type=service_type, worker=worker, route=route, points=points)


async def _position(
    db: AsyncSession, working: _Working, at: tuple[float, float], minutes_ago: int
) -> None:
    db.add(
        WorkerPosition(
            field_worker_id=working.worker.id,
            route_id=working.route.id,
            latitude=at[0],
            longitude=at[1],
            reported_at=_NOW - timedelta(minutes=minutes_ago),
        )
    )
    await db.commit()


async def _route_state(db: AsyncSession, route: Route) -> tuple[RouteStatus, list[RouteStop]]:
    await db.refresh(route)
    stops = list(
        await db.scalars(
            select(RouteStop)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.order_index)
            .execution_options(populate_existing=True)
        )
    )
    return route.status, stops


def _assert_released(stops: list[RouteStop], reason: ReleaseReason) -> None:
    assert [stop.status for stop in stops] == [RouteStopStatus.RELEASED] * len(stops)
    assert {stop.release_reason for stop in stops} == {reason}


async def test_outside_campus_for_ten_minutes_releases(db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    working = await _working(db_session)
    await _position(db_session, working, _INSIDE, minutes_ago=25)
    await _position(db_session, working, _OUTSIDE, minutes_ago=11)
    await _position(db_session, working, _OUTSIDE, minutes_ago=1)

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, stops = await _route_state(db_session, working.route)
    assert route_status is RouteStatus.PAUSED
    _assert_released(stops, ReleaseReason.LEFT_CAMPUS)
    assert [(a.kind, a.severity, a.route_id) for a in alerts] == [
        (AlertKind.BATCH_RELEASED, AlertSeverity.WARNING, working.route.id)
    ]
    assert alerts[0].dedup_key == f"BATCH_RELEASED:{working.route.id}:20261020T1300"
    assert alerts[0].field_worker_id == working.worker.id
    assert "fora do campus há mais de 10 min" in alerts[0].description
    assert "2 ponto(s)" in alerts[0].description
    notified = list(await db_session.scalars(select(Notification)))
    assert [(n.user_id, n.kind) for n in notified] == [(manager.id, "ALERT")]


async def test_sparse_reporter_just_outside_not_released(db_session: AsyncSession) -> None:
    # A worker without background permission: the last fix inside is 25 min old and the next
    # one, 1 min old, is just past the rectangle edge. Outside for a minute, not for ten.
    working = await _working(db_session)
    await _position(db_session, working, _INSIDE, minutes_ago=25)
    await _position(db_session, working, _OUTSIDE, minutes_ago=1)

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, _ = await _route_state(db_session, working.route)
    assert alerts == []
    assert route_status is RouteStatus.IN_PROGRESS


async def test_checkin_after_outside_fix_counts_as_inside(db_session: AsyncSession) -> None:
    # Points are on campus: a check-in after the outside fixes puts the worker back inside.
    working = await _working(db_session)
    await _position(db_session, working, _OUTSIDE, minutes_ago=20)
    await _position(db_session, working, _OUTSIDE, minutes_ago=15)
    await make_check_in(
        db_session,
        stop=working.route.stops[0],
        worker=working.worker,
        scanned_at=_NOW - timedelta(minutes=12),
    )
    await _position(db_session, working, _OUTSIDE, minutes_ago=5)

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, _ = await _route_state(db_session, working.route)
    assert alerts == []
    assert route_status is RouteStatus.IN_PROGRESS


async def test_scan_release_is_audited_per_stop(db_session: AsyncSession) -> None:
    working = await _working(db_session, allocated_minutes_ago=60)
    stop_ids = {stop.id for stop in working.route.stops}

    await run_release_scan(db_session, now=_NOW)

    audits = list(
        await db_session.scalars(select(AuditTrail).where(AuditTrail.action == "release"))
    )
    assert {a.entity_id for a in audits} == stop_ids
    assert {(a.actor_id, a.entity_type) for a in audits} == {(None, "route_stop")}
    assert audits[0].after_data == {
        "status": "RELEASED",
        "release_reason": "LOCATION_SILENCE",
        "released_at": _NOW.isoformat(),
        "route_id": str(working.route.id),
    }


async def test_brief_exit_does_not_release(db_session: AsyncSession) -> None:
    working = await _working(db_session)
    await _position(db_session, working, _INSIDE, minutes_ago=12)
    await _position(db_session, working, _OUTSIDE, minutes_ago=8)
    await _position(db_session, working, _INSIDE, minutes_ago=5)
    await _position(db_session, working, _OUTSIDE, minutes_ago=1)

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, stops = await _route_state(db_session, working.route)
    assert alerts == []
    assert route_status is RouteStatus.IN_PROGRESS
    assert {stop.status for stop in stops} == {RouteStopStatus.PENDING}


async def test_outside_position_before_the_last_batch_does_not_release(
    db_session: AsyncSession,
) -> None:
    # The worker was outside, came back and got a new batch on campus without a position since:
    # the stale outside fix says nothing about the batch they hold now.
    working = await _working(db_session, allocated_minutes_ago=5)
    await _position(db_session, working, _OUTSIDE, minutes_ago=20)

    assert await run_release_scan(db_session, now=_NOW) == []
    route_status, _ = await _route_state(db_session, working.route)
    assert route_status is RouteStatus.IN_PROGRESS


@pytest.mark.parametrize(("minutes_ago", "released"), [(44, False), (46, True)])
async def test_silence_releases_after_45_minutes(
    db_session: AsyncSession, minutes_ago: int, released: bool
) -> None:
    working = await _working(db_session, allocated_minutes_ago=60)
    await _position(db_session, working, _INSIDE, minutes_ago=minutes_ago)

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, stops = await _route_state(db_session, working.route)
    if not released:
        assert alerts == []
        assert route_status is RouteStatus.IN_PROGRESS
        return
    assert route_status is RouteStatus.PAUSED
    _assert_released(stops, ReleaseReason.LOCATION_SILENCE)
    assert [a.kind for a in alerts] == [AlertKind.BATCH_RELEASED]
    assert "sem enviar localização há mais de 45 min" in alerts[0].description


async def test_checkin_counts_as_activity(db_session: AsyncSession) -> None:
    # A worker without background-location permission works with the app open: no position for
    # 50 min, but a check-in 10 min ago — they are working, never silent.
    working = await _working(db_session, allocated_minutes_ago=60)
    await _position(db_session, working, _INSIDE, minutes_ago=50)
    await make_check_in(
        db_session,
        stop=working.route.stops[0],
        worker=working.worker,
        scanned_at=_NOW - timedelta(minutes=10),
    )
    await db_session.commit()

    alerts = await run_release_scan(db_session, now=_NOW)

    route_status, _ = await _route_state(db_session, working.route)
    assert alerts == []
    assert route_status is RouteStatus.IN_PROGRESS


@pytest.mark.parametrize(("allocated_minutes_ago", "released"), [(44, False), (46, True)])
async def test_silence_counted_from_last_allocation_when_no_position(
    db_session: AsyncSession, allocated_minutes_ago: int, released: bool
) -> None:
    working = await _working(db_session, allocated_minutes_ago=allocated_minutes_ago)

    await run_release_scan(db_session, now=_NOW)

    route_status, stops = await _route_state(db_session, working.route)
    if not released:
        assert route_status is RouteStatus.IN_PROGRESS
        return
    assert route_status is RouteStatus.PAUSED
    _assert_released(stops, ReleaseReason.LOCATION_SILENCE)


async def test_route_without_a_live_stop_is_left_alone(db_session: AsyncSession) -> None:
    # The batch is all done and the worker has not asked for the next one yet: nothing to give
    # back, so no release and no alert however long the silence.
    working = await _working(db_session, allocated_minutes_ago=120, rooms=1)
    working.route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()

    assert await run_release_scan(db_session, now=_NOW) == []
    route_status, _ = await _route_state(db_session, working.route)
    assert route_status is RouteStatus.IN_PROGRESS


async def test_end_of_day_closes_routes_and_raises_points_unserved_once(
    db_session: AsyncSession,
) -> None:
    await _manager(db_session)
    working = await _working(db_session, allocated_minutes_ago=5, now=_FIVE_PM, rooms=2)
    working.route.stops[0].status = RouteStopStatus.DONE
    paused = await _working(
        db_session,
        allocated_minutes_ago=5,
        now=_FIVE_PM,
        rooms=1,
        service_type=working.service_type,
    )
    paused.route.status = RouteStatus.PAUSED
    paused.route.stops[0].status = RouteStopStatus.RELEASED
    other_type = await make_service_type(
        db_session, company_name="Verde Vivo", type_name="Jardinagem"
    )
    await make_points(db_session, service_type=other_type, names=["Jardim 1", "Jardim 2"])
    await make_points(db_session, service_type=None, names=["Sem tipo"])
    await db_session.commit()

    alerts = await run_release_scan(db_session, now=_FIVE_PM)
    again = await run_release_scan(db_session, now=_FIVE_PM + timedelta(minutes=1))

    route_status, stops = await _route_state(db_session, working.route)
    assert route_status is RouteStatus.DONE
    assert [stop.status for stop in stops] == [RouteStopStatus.DONE, RouteStopStatus.RELEASED]
    assert stops[1].release_reason is ReleaseReason.END_OF_DAY
    paused_status, _ = await _route_state(db_session, paused.route)
    assert paused_status is RouteStatus.DONE
    assert [(a.kind, a.route_id, a.dedup_key) for a in alerts] == [
        (AlertKind.POINTS_UNSERVED, None, f"POINTS_UNSERVED:{_TODAY}")
    ]
    # 1 Limpeza point of the working route + 1 of the paused one; 2 Jardinagem; the untyped
    # point is in nobody's pool and does not count.
    assert alerts[0].description == (
        "Expediente encerrado com 4 ponto(s) não atendido(s) hoje: "
        "Jardinagem (Verde Vivo): 2; Limpeza (Limpa Tudo): 2."
    )
    assert again == []
    assert (
        await db_session.scalar(select(Alert.id).where(Alert.kind != AlertKind.POINTS_UNSERVED))
        is None
    )


async def test_end_of_day_with_every_point_done_raises_nothing(db_session: AsyncSession) -> None:
    working = await _working(db_session, allocated_minutes_ago=5, now=_FIVE_PM, rooms=1)
    working.route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()

    assert await run_release_scan(db_session, now=_FIVE_PM) == []
    route_status, _ = await _route_state(db_session, working.route)
    assert route_status is RouteStatus.DONE


async def test_accepted_appeal_counts_the_point_as_served(db_session: AsyncSession) -> None:
    # Spec §8.7: a point is served on the day when it has a DONE stop or an accepted appeal on
    # a stop of that day. A rejected (or pending) appeal leaves it unserved.
    working = await _working(db_session, allocated_minutes_ago=5, now=_FIVE_PM, rooms=3)
    appealed, rejected, _still_pending = working.route.stops
    for stop, status in ((appealed, AppealStatus.ACCEPTED), (rejected, AppealStatus.REJECTED)):
        stop.status = RouteStopStatus.RELEASED
        stop.release_reason = ReleaseReason.LEFT_CAMPUS
        stop.released_at = _FIVE_PM - timedelta(hours=2)
        db_session.add(
            Appeal(
                route_stop_id=stop.id,
                field_worker_id=working.worker.id,
                reason=AppealReason.LEFT_BRIEFLY,
                status=status,
                idempotency_key=uuid.uuid4(),
            )
        )
    await db_session.commit()

    alerts = await run_release_scan(db_session, now=_FIVE_PM)

    assert [alert.description for alert in alerts] == [
        "Expediente encerrado com 2 ponto(s) não atendido(s) hoje: Limpeza (Limpa Tudo): 2."
    ]


async def test_saturday_scan_does_nothing(db_session: AsyncSession) -> None:
    saturday_five_pm = _SATURDAY + timedelta(hours=7)
    working = await _working(db_session, allocated_minutes_ago=120, now=saturday_five_pm)

    alerts = await run_release_scan(db_session, now=saturday_five_pm)

    route_status, stops = await _route_state(db_session, working.route)
    assert alerts == []
    assert route_status is RouteStatus.IN_PROGRESS
    assert {stop.status for stop in stops} == {RouteStopStatus.PENDING}


async def test_released_point_back_in_pool(db_session: AsyncSession) -> None:
    working = await _working(db_session, allocated_minutes_ago=60)
    company_id = working.worker.contractor_company_id
    assert (
        await free_points(db_session, company_id=company_id, day=_TODAY, exclude_point_ids=set())
        == []
    )

    await run_release_scan(db_session, now=_NOW)

    pool = await free_points(db_session, company_id=company_id, day=_TODAY, exclude_point_ids=set())
    assert {point.id for point in pool} == {point.id for point in working.points}
