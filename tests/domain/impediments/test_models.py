from datetime import UTC, datetime
from typing import cast

from app.domain.impediments.models import Impediment, ImpedimentReason, ImpedimentScope
from sqlalchemy import Table, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import seed_route_with_one_stop


async def test_stop_scope_impediment_round_trips(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)

    impediment = Impediment(
        route_id=stop.route_id,
        route_stop_id=stop.id,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.ACCESS_BLOCKED,
        reported_by=worker.id,
        reported_at=datetime.now(UTC),
    )
    db_session.add(impediment)
    await db_session.flush()
    await db_session.refresh(impediment)

    assert impediment.route_stop_id == stop.id
    assert impediment.status.value == "PENDING"
    assert impediment.received_at is not None
    assert impediment.resolved_at is None
    assert impediment.resolved_by is None


async def test_route_remainder_scope_has_no_route_stop(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)

    impediment = Impediment(
        route_id=stop.route_id,
        route_stop_id=None,
        scope=ImpedimentScope.ROUTE_REMAINDER,
        reason=ImpedimentReason.HEALTH_ISSUE,
        reported_by=worker.id,
        reported_at=datetime.now(UTC),
    )
    db_session.add(impediment)
    await db_session.flush()
    await db_session.refresh(impediment)

    assert impediment.route_stop_id is None
    assert impediment.scope is ImpedimentScope.ROUTE_REMAINDER


async def test_multiple_impediments_allowed_for_same_route(db_session: AsyncSession) -> None:
    """No unique index on route_id: several impediments can pile up for the same route over
    time (e.g. one resolved, another reported later), unlike Form.service_type_id."""
    stop, worker = await seed_route_with_one_stop(db_session)
    table = cast(Table, Impediment.__table__)
    unique_indexes = [ix for ix in table.indexes if ix.unique]
    unique_constraints = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert unique_indexes == []
    assert unique_constraints == []

    first = Impediment(
        route_id=stop.route_id,
        route_stop_id=stop.id,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.OTHER,
        reported_by=worker.id,
        reported_at=datetime.now(UTC),
    )
    second = Impediment(
        route_id=stop.route_id,
        route_stop_id=stop.id,
        scope=ImpedimentScope.STOP,
        reason=ImpedimentReason.SITE_REFUSAL,
        reported_by=worker.id,
        reported_at=datetime.now(UTC),
    )
    db_session.add_all([first, second])
    await db_session.flush()

    rows = await db_session.scalars(select(Impediment).where(Impediment.route_id == stop.route_id))
    assert len(rows.all()) == 2
