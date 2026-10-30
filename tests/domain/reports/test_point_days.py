"""`point_day_rows` (spec 2026-10-20-alocacao-automatica §8.7 "Base de cálculo") against the real
testcontainer: one row per typed point per service day it existed, counted once — done (a DONE
stop that day), done by appeal (an accepted appeal on a stop of that day) or unserved."""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.catalog.models import FieldWorker, Floor, ServicePoint, ServiceType
from app.domain.reports.point_days import PointDay, point_day_rows
from app.domain.routing.models import ReleaseReason, RouteStop, RouteStopStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

pytestmark = pytest.mark.asyncio

# The week of Monday 2026-10-19; Saturday 24 and Sunday 25 are not service days by default.
_MON = date(2026, 10, 19)
_TUE = _MON + timedelta(days=1)
_WED = _MON + timedelta(days=2)
_FRI = _MON + timedelta(days=4)
_SUN = _MON + timedelta(days=6)
# Long before the week: every point of these tests exists on every day of it.
_LONG_AGO = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


async def _typed_points(
    db: AsyncSession, names: Sequence[str], *, service_type: ServiceType | None = None
) -> tuple[ServiceType, list[ServicePoint]]:
    service_type = service_type or await make_service_type(db)
    points = await make_points(db, service_type=service_type, names=names)
    for point in points:
        point.created_at = _LONG_AGO
    await db.flush()
    return service_type, points


async def _worker(db: AsyncSession, service_type: ServiceType, name: str) -> FieldWorker:
    assert service_type.contractor_company is not None
    return await make_worker(db, company=service_type.contractor_company, full_name=name)


async def _rows(db: AsyncSession, date_from: date, date_to: date) -> list[PointDay]:
    await db.commit()
    return await point_day_rows(db, date_from=date_from, date_to=date_to)


async def test_released_then_done_by_other_counts_once_done(db_session: AsyncSession) -> None:
    service_type, (point,) = await _typed_points(db_session, ["Sala 101"])
    ana = await _worker(db_session, service_type, "Ana")
    bruno = await _worker(db_session, service_type, "Bruno")
    released = await make_route(db_session, worker=ana, day=_TUE, points=[point])
    released.stops[0].status = RouteStopStatus.RELEASED
    released.stops[0].release_reason = ReleaseReason.LEFT_CAMPUS
    released.stops[0].released_at = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
    done = await make_route(db_session, worker=bruno, day=_TUE, points=[point])
    done.stops[0].status = RouteStopStatus.DONE

    rows = await _rows(db_session, _TUE, _TUE)

    assert len(rows) == 1
    row = rows[0]
    assert (row.day, row.service_point_id, row.outcome) == (_TUE, point.id, "DONE")
    assert row.done_by == bruno.id
    assert row.released_count == 1
    assert row.release_reasons == (ReleaseReason.LEFT_CAMPUS,)
    assert row.impeded_count == 0


async def test_accepted_appeal_counts_done_by_appeal(db_session: AsyncSession) -> None:
    service_type, (accepted, rejected) = await _typed_points(db_session, ["Sala 1", "Sala 2"])
    ana = await _worker(db_session, service_type, "Ana")
    route = await make_route(db_session, worker=ana, day=_TUE, points=[accepted, rejected])
    for stop, status in zip(
        route.stops, (AppealStatus.ACCEPTED, AppealStatus.REJECTED), strict=True
    ):
        stop.status = RouteStopStatus.RELEASED
        stop.release_reason = ReleaseReason.LOCATION_SILENCE
        stop.released_at = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
        db_session.add(
            Appeal(
                route_stop_id=stop.id,
                field_worker_id=ana.id,
                reason=AppealReason.NO_SIGNAL,
                status=status,
                idempotency_key=uuid.uuid4(),
            )
        )

    rows = {row.service_point_id: row for row in await _rows(db_session, _TUE, _TUE)}

    assert rows[accepted.id].outcome == "DONE_BY_APPEAL"
    assert rows[accepted.id].done_by == ana.id
    assert rows[accepted.id].released_count == 1
    assert rows[rejected.id].outcome == "UNSERVED"
    assert rows[rejected.id].done_by is None


async def test_weekend_days_skipped(db_session: AsyncSession) -> None:
    _, (point,) = await _typed_points(db_session, ["Sala 101"])

    rows = await _rows(db_session, _MON, _SUN)

    assert [row.day for row in rows] == [_MON + timedelta(days=i) for i in range(5)]
    assert {row.service_point_id for row in rows} == {point.id}
    assert {row.outcome for row in rows} == {"UNSERVED"}
    assert await point_day_rows(db_session, date_from=_SUN - timedelta(days=1), date_to=_SUN) == []


async def test_point_created_midweek_counted_from_then(db_session: AsyncSession) -> None:
    _, (point,) = await _typed_points(db_session, ["Sala 101"])
    # Wednesday 22:30 on campus — already Thursday in UTC: the campus day decides.
    point.created_at = datetime(2026, 10, 22, 1, 30, tzinfo=UTC)

    rows = await _rows(db_session, _MON, _FRI)

    assert [row.day for row in rows] == [_WED, _WED + timedelta(days=1), _FRI]


async def test_untyped_point_not_counted(db_session: AsyncSession) -> None:
    service_type, (typed,) = await _typed_points(db_session, ["Tipado"])
    company_less = ServiceType(name="Sem empresa", average_duration_minutes=10)
    db_session.add(company_less)
    await db_session.flush()
    untyped = await make_points(db_session, service_type=None, names=["Sem tipo"])
    orphan = await make_points(db_session, service_type=company_less, names=["Tipo sem empresa"])
    for point in (*untyped, *orphan):
        point.created_at = _LONG_AGO

    rows = await _rows(db_session, _TUE, _TUE)

    assert [(row.service_point_id, row.service_type_id) for row in rows] == [
        (typed.id, service_type.id)
    ]
    assert rows[0].company_id == service_type.contractor_company_id


async def test_impediment_release_counts_as_impeded(db_session: AsyncSession) -> None:
    service_type, (point,) = await _typed_points(db_session, ["Sala 101"])
    ana = await _worker(db_session, service_type, "Ana")
    bruno = await _worker(db_session, service_type, "Bruno")
    released = [
        (ana, ReleaseReason.IMPEDIMENT, datetime(2026, 10, 20, 12, 0, tzinfo=UTC)),
        (bruno, ReleaseReason.END_OF_DAY, datetime(2026, 10, 20, 20, 0, tzinfo=UTC)),
    ]
    for worker, reason, released_at in released:
        route = await make_route(db_session, worker=worker, day=_TUE, points=[point])
        route.stops[0].status = RouteStopStatus.RELEASED
        route.stops[0].release_reason = reason
        route.stops[0].released_at = released_at

    (row,) = await _rows(db_session, _TUE, _TUE)

    assert row.outcome == "UNSERVED"
    assert row.release_reasons == (ReleaseReason.IMPEDIMENT, ReleaseReason.END_OF_DAY)
    assert row.released_count == 2
    assert row.impeded_count == 1


async def test_filters_narrow_the_rows(db_session: AsyncSession) -> None:
    sweeping, (sala_1, sala_2) = await _typed_points(db_session, ["Sala 1", "Sala 2"])
    garden = await make_service_type(db_session, company_name="Verde", type_name="Jardinagem")
    _, (jardim,) = await _typed_points(db_session, ["Jardim"], service_type=garden)
    ana = await _worker(db_session, sweeping, "Ana")
    route = await make_route(db_session, worker=ana, day=_TUE, points=[sala_2])
    route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()

    async def ids(**filters: uuid.UUID) -> set[uuid.UUID]:
        rows = await point_day_rows(db_session, date_from=_TUE, date_to=_TUE, **filters)
        return {row.service_point_id for row in rows}

    assert await ids() == {sala_1.id, sala_2.id, jardim.id}
    assert sweeping.contractor_company_id is not None
    assert await ids(company_id=sweeping.contractor_company_id) == {sala_1.id, sala_2.id}
    assert await ids(service_type_id=sweeping.id) == {sala_1.id, sala_2.id}
    floor = await db_session.get(Floor, jardim.floor_id)
    assert floor is not None
    assert await ids(building_id=floor.building_id) == {jardim.id}
    # A worker's rows are the point-days they held a stop for.
    assert await ids(field_worker_id=ana.id) == {sala_2.id}


async def test_inactive_point_leaves_the_expected_set_but_keeps_its_history(
    db_session: AsyncSession,
) -> None:
    service_type, (kept, switched_off) = await _typed_points(db_session, ["Sala 1", "Sala 2"])
    ana = await _worker(db_session, service_type, "Ana")
    route = await make_route(db_session, worker=ana, day=_TUE, points=[switched_off])
    route.stops[0].status = RouteStopStatus.DONE
    switched_off.active = False

    rows = await _rows(db_session, _MON, _FRI)

    assert {row.service_point_id for row in rows} == {kept.id}
    stops = list(
        await db_session.scalars(
            select(RouteStop).where(RouteStop.service_point_id == switched_off.id)
        )
    )
    assert [(stop.service_date, stop.status) for stop in stops] == [(_TUE, RouteStopStatus.DONE)]
