"""Impediment service (Task 2) — report, PENDING queue and dismiss, over the `db_session`
fixture (spec RF16-RF21, Etapa 8)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.impediments.models import ImpedimentReason, ImpedimentScope
from app.domain.impediments.service import (
    AlreadyResolved,
    RouteNotInProgress,
    StopNotReassignable,
    create_impediment,
    dismiss_impediment,
    list_pending,
)
from app.domain.routing.models import Route, RouteStatus, RouteStopStatus
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
    )

    assert impediment.route_id == route.id
    assert impediment.route_stop_id == stop.id
    assert impediment.scope is ImpedimentScope.STOP
    assert impediment.reason is ImpedimentReason.ACCESS_BLOCKED
    assert impediment.note == "Portão trancado"
    assert impediment.reported_by == worker.id
    assert impediment.reported_at == reported_at
    assert impediment.status.value == "PENDING"


async def test_create_impediment_stop_scope_rejects_route_not_in_progress(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    route.status = RouteStatus.PLANNED
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
    )

    assert impediment.route_stop_id is None
    assert impediment.scope is ImpedimentScope.ROUTE_REMAINDER


async def test_create_impediment_route_remainder_scope_rejects_route_not_in_progress(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    route.status = RouteStatus.CANCELLED
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
        )


async def test_list_pending_returns_only_pending_ordered_oldest_first(
    db_session: AsyncSession,
) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    now = datetime.now(UTC)

    newer = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by_id=worker.id,
        reported_at=now,
    )
    older = await create_impediment(
        db_session,
        route=route,
        route_stop=None,
        scope=ImpedimentScope.ROUTE_REMAINDER,
        reason=ImpedimentReason.SITE_REFUSAL,
        note=None,
        reported_by_id=worker.id,
        reported_at=now - timedelta(hours=1),
    )
    resolved = await create_impediment(
        db_session,
        route=route,
        route_stop=None,
        scope=ImpedimentScope.ROUTE_REMAINDER,
        reason=ImpedimentReason.SITE_REFUSAL,
        note=None,
        reported_by_id=worker.id,
        reported_at=now - timedelta(hours=2),
    )
    actor_id = worker.user_id or uuid.uuid4()
    await dismiss_impediment(db_session, impediment=resolved, actor_id=actor_id)

    pending = await list_pending(db_session)

    assert [p.id for p in pending] == [older.id, newer.id]


async def test_dismiss_impediment_marks_resolved(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by_id=worker.id,
        reported_at=datetime.now(UTC),
    )
    actor_id = worker.user_id
    assert actor_id is not None

    resolved = await dismiss_impediment(db_session, impediment=impediment, actor_id=actor_id)

    assert resolved.status.value == "RESOLVED"
    assert resolved.resolved_by == actor_id
    assert resolved.resolved_at is not None


async def test_dismiss_impediment_twice_raises_already_resolved(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    impediment = await create_impediment(
        db_session,
        route=route,
        route_stop=stop,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by_id=worker.id,
        reported_at=datetime.now(UTC),
    )
    actor_id = worker.user_id
    assert actor_id is not None
    await dismiss_impediment(db_session, impediment=impediment, actor_id=actor_id)

    with pytest.raises(AlreadyResolved):
        await dismiss_impediment(db_session, impediment=impediment, actor_id=actor_id)
