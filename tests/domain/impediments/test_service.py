"""Impediment service — report (which releases the point(s) back to the pool), the manager's
list and the "ciente" acknowledgement, over the `db_session` fixture (spec RF16-RF17,
2026-10-20-alocacao-automatica §7.1)."""

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.identity.models import User, UserRole
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.impediments.service import (
    RouteNotInProgress,
    StopNotReassignable,
    acknowledge_impediment,
    create_impediment,
    find_impediments,
)
from app.domain.monitoring.push import LoggingPushSender
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop


async def test_create_impediment_with_stop_scope_records_fields(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    reported_at = datetime.now(UTC)

    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.ACCESS_BLOCKED,
        note="Portão trancado",
        reported_by_id=worker.id,
        reported_at=reported_at,
        now=reported_at,
        push=LoggingPushSender(),
    )

    assert impediment.route_id == route.id
    assert impediment.route_stop_id == stop.id
    assert impediment.scope is ImpedimentScope.STOP
    assert impediment.reason is ImpedimentReason.ACCESS_BLOCKED
    assert impediment.note == "Portão trancado"
    assert impediment.reported_by == worker.id
    assert impediment.reported_at == reported_at
    assert impediment.status.value == "PENDING"
    await db_session.refresh(stop)
    assert stop.status is RouteStopStatus.RELEASED
    assert stop.release_reason is ReleaseReason.IMPEDIMENT
    assert stop.released_at == reported_at


async def test_create_impediment_stop_scope_rejects_route_not_in_progress(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    route.status = RouteStatus.PAUSED
    await db_session.flush()

    with pytest.raises(RouteNotInProgress):
        await create_impediment(
            db_session,
            route=route,
            route_stop=stop,
            scope=ImpedimentScope.STOP,
            reason=ImpedimentReason.OTHER,
            note=None,
            reported_by_id=worker.id,
            reported_at=datetime.now(UTC),
            now=datetime.now(UTC),
            push=LoggingPushSender(),
        )


async def test_create_impediment_stop_scope_rejects_done_stop(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    stop.status = RouteStopStatus.DONE
    await db_session.flush()

    with pytest.raises(StopNotReassignable):
        await create_impediment(
            db_session,
            route=route,
            route_stop=stop,
            scope=ImpedimentScope.STOP,
            reason=ImpedimentReason.OTHER,
            note=None,
            reported_by_id=worker.id,
            reported_at=datetime.now(UTC),
            now=datetime.now(UTC),
            push=LoggingPushSender(),
        )


async def test_create_impediment_stop_scope_accepts_in_progress_stop(
    db_session: AsyncSession,
) -> None:
    # A worker who checked in (stop → IN_PROGRESS) and then hit a wall must still be able
    # to signal the impediment (F1 fix, Ruling P3).
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    stop.status = RouteStopStatus.IN_PROGRESS
    await db_session.flush()

    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.HEALTH_ISSUE,
        note=None,
        reported_by_id=worker.id,
        reported_at=datetime.now(UTC),
        now=datetime.now(UTC),
        push=LoggingPushSender(),
    )

    assert impediment.route_stop_id == stop.id
    assert impediment.status.value == "PENDING"


async def test_create_impediment_route_remainder_scope_has_no_route_stop(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    # Even with the only stop already DONE (nothing pending on the route right now), a
    # ROUTE_REMAINDER report is still recorded — no per-stop validation applies to it.
    stop.status = RouteStopStatus.DONE
    await db_session.flush()

    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=None,
        scope=ImpedimentScope.ROUTE_REMAINDER,
        reason=ImpedimentReason.HEALTH_ISSUE,
        note=None,
        reported_by_id=worker.id,
        reported_at=datetime.now(UTC),
        now=datetime.now(UTC),
        push=LoggingPushSender(),
    )

    assert impediment.route_stop_id is None
    assert impediment.scope is ImpedimentScope.ROUTE_REMAINDER


async def test_create_impediment_route_remainder_scope_rejects_route_not_in_progress(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    route.status = RouteStatus.DONE
    await db_session.flush()

    with pytest.raises(RouteNotInProgress):
        await create_impediment(
            db_session,
            route=route,
            route_stop=None,
            scope=ImpedimentScope.ROUTE_REMAINDER,
            reason=ImpedimentReason.HEALTH_ISSUE,
            note=None,
            reported_by_id=worker.id,
            reported_at=datetime.now(UTC),
            now=datetime.now(UTC),
            push=LoggingPushSender(),
        )


async def test_list_impediments_pending_first_then_newest_first(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    now = datetime.now(UTC)

    async def remainder(hours_ago: int) -> Impediment:
        route.status = RouteStatus.IN_PROGRESS  # each remainder report pauses the route
        return await create_impediment(
            db_session,
            route=route,
            route_stop=None,
            scope=ImpedimentScope.ROUTE_REMAINDER,
            reason=ImpedimentReason.OTHER,
            note=None,
            reported_by_id=worker.id,
            reported_at=now - timedelta(hours=hours_ago),
            now=now,
            push=LoggingPushSender(),
        )

    older = await remainder(2)
    newer = await remainder(1)
    resolved = await remainder(0)
    resolved.status = ImpedimentStatus.RESOLVED
    await db_session.flush()

    every = await find_impediments(db_session, status=None)
    pending = await find_impediments(db_session, status=ImpedimentStatus.PENDING)

    assert [i.id for i in every] == [newer.id, older.id, resolved.id]
    assert [i.id for i in pending] == [newer.id, older.id]


async def test_acknowledge_impediment_resolves_once(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    manager = User(
        name="Larissa", email="larissa@pu.ufcg.edu.br", password_hash="x", role=UserRole.MANAGER
    )
    db_session.add(manager)
    await db_session.flush()
    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by_id=worker.id,
        reported_at=datetime.now(UTC),
        now=datetime.now(UTC),
        push=LoggingPushSender(),
    )

    acknowledged = await acknowledge_impediment(db_session, impediment=impediment, actor=manager)
    first_at = acknowledged.resolved_at
    again = await acknowledge_impediment(db_session, impediment=impediment, actor=manager)

    assert acknowledged.status is ImpedimentStatus.RESOLVED
    assert acknowledged.resolved_by == manager.id
    assert first_at is not None
    assert again.resolved_at == first_at


async def test_create_impediment_on_a_stop_finished_meanwhile_is_refused(
    db_session: AsyncSession,
) -> None:
    # The endpoint loaded the stop PENDING; a check-out committed DONE before the release ran.
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    await db_session.execute(
        update(RouteStop)
        .where(RouteStop.id == stop.id)
        .values(status=RouteStopStatus.DONE)
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(StopNotReassignable):
        await create_impediment(
            db_session,
            route=route,
            route_stop=stop,
            scope=ImpedimentScope.STOP,
            reason=ImpedimentReason.OTHER,
            note=None,
            reported_by_id=worker.id,
            reported_at=datetime.now(UTC),
            now=datetime.now(UTC),
            push=LoggingPushSender(),
        )
