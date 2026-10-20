"""RF45 — `build_compliance_report` on the point-by-day model (spec 2026-10-20-alocacao-automatica
§8.7) against the real testcontainer.

One fixed scenario over seven service days from Thu 2026-08-27 to Fri 2026-09-04 — two ISO weeks
and two months, so DAILY, WEEKLY and MONTHLY each produce more than one bucket. Three typed
points: P1 (Limpeza, Bloco 1) and P2 (Jardinagem, Bloco 2) exist all along, P3 (Limpeza, Bloco 1)
from Wed 09-02. Executions and alerts hang off some of them, and two alerts have no point (a
batch release on a route, a day-level POINTS_UNSERVED). Every indicator is asserted per bucket and
in the totals row against hand-computed constants, plus the filters — the outcome axis and a
worker credited only with their own work — and the resolved `filters_applied`.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import get_args

import pytest
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.catalog.models import Building, FieldWorker, Floor, ServicePoint
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
)
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.reports.compliance import (
    OUTCOME_LABELS,
    ComplianceReport,
    ComplianceRow,
    ReportGranularity,
    build_compliance_report,
)
from app.domain.reports.filters import ReportFilters
from app.domain.reports.point_days import PointDayOutcome
from app.domain.routing.models import ReleaseReason, RouteStop, RouteStopStatus
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

pytestmark = pytest.mark.asyncio

_FROM = date(2026, 8, 27)
_TO = date(2026, 9, 4)
_LONG_AGO = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
_RANGE = ReportFilters(date_from=_FROM, date_to=_TO)

_METRICS = (
    "points_total",
    "points_done",
    "points_done_by_appeal",
    "points_unserved",
    "points_released",
    "points_impeded",
    "released_route_remainder",
    "released_left_campus",
    "released_location_silence",
    "released_end_of_day",
    "executions_total",
    "executions_with_evidence",
    "executions_flagged",
    "alerts_total",
)


@dataclass
class _Ids:
    walter: uuid.UUID
    yuri: uuid.UUID
    cleaning_company: uuid.UUID
    cleaning_type: uuid.UUID
    building_1: uuid.UUID
    building_1_name: str


async def _stop(
    db: AsyncSession,
    worker: FieldWorker,
    day: date,
    point: ServicePoint,
    *,
    status: RouteStopStatus,
    reason: ReleaseReason | None = None,
) -> RouteStop:
    route = await make_route(db, worker=worker, day=day, points=[point])
    stop = route.stops[0]
    stop.status = status
    if reason is not None:
        stop.release_reason = reason
        stop.released_at = datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC)
    return stop


def _at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


async def _execution(
    db: AsyncSession, stop: RouteStop, worker: FieldWorker, *, evidence: bool, flagged: bool = False
) -> None:
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=_at(stop.service_date, 13),
        synced_at=_at(stop.service_date, 13),
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
        review_status=(
            ExecutionReviewStatus.PENDING_REVIEW if flagged else ExecutionReviewStatus.NONE
        ),
    )
    db.add(execution)
    await db.flush()
    if evidence:
        db.add(
            EvidenceItem(
                execution_id=execution.id,
                kind=EvidenceKind.NOTE,
                text_body="feito",
                captured_at=_at(stop.service_date, 13),
            )
        )


def _alert(
    db: AsyncSession,
    kind: AlertKind,
    *,
    stop: RouteStop | None = None,
    route_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> None:
    db.add(
        Alert(
            kind=kind,
            severity=AlertSeverity.WARNING,
            dedup_key=f"{kind}:{uuid.uuid4()}",
            description="alerta",
            route_id=stop.route_id if stop is not None else route_id,
            route_stop_id=stop.id if stop is not None else None,
            **({"created_at": created_at} if created_at is not None else {}),
        )
    )


async def _seed(db: AsyncSession) -> _Ids:
    cleaning = await make_service_type(db, company_name="Limpa Tudo", type_name="Limpeza")
    garden = await make_service_type(db, company_name="Verde", type_name="Jardinagem")
    p1, p3 = await make_points(db, service_type=cleaning, names=["P1", "P3"])
    (p2,) = await make_points(db, service_type=garden, names=["P2"])
    for point in (p1, p2):
        point.created_at = _LONG_AGO
    p3.created_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert cleaning.contractor_company is not None
    assert garden.contractor_company is not None
    walter = await make_worker(db, company=cleaning.contractor_company, full_name="Walter")
    xavier = await make_worker(db, company=garden.contractor_company, full_name="Xavier")
    yuri = await make_worker(db, company=cleaning.contractor_company, full_name="Yuri")
    released, done = RouteStopStatus.RELEASED, RouteStopStatus.DONE

    # 08-27: Yuri leaves the campus with P1 halfway (an execution without evidence, a
    # NO_EVIDENCE alert on his stop), Walter does it.
    left = await _stop(
        db, yuri, date(2026, 8, 27), p1, status=released, reason=ReleaseReason.LEFT_CAMPUS
    )
    await _execution(db, left, yuri, evidence=False)
    _alert(db, AlertKind.NO_EVIDENCE, stop=left)
    await _execution(
        db, await _stop(db, walter, date(2026, 8, 27), p1, status=done), walter, evidence=True
    )
    # 08-28: Xavier reports P2 impeded.
    impeded = await _stop(
        db, xavier, date(2026, 8, 28), p2, status=released, reason=ReleaseReason.IMPEDIMENT
    )
    _alert(db, AlertKind.POINT_IMPEDED, stop=impeded)
    # 09-01: Walter goes silent with P1 (a batch alert on his route, no stop) and wins the
    # appeal — its execution waits for review.
    appealed = await _stop(
        db, walter, date(2026, 9, 1), p1, status=released, reason=ReleaseReason.LOCATION_SILENCE
    )
    db.add(
        Appeal(
            route_stop_id=appealed.id,
            field_worker_id=walter.id,
            reason=AppealReason.NO_SIGNAL,
            status=AppealStatus.ACCEPTED,
            idempotency_key=uuid.uuid4(),
        )
    )
    await _execution(db, appealed, walter, evidence=False, flagged=True)
    _alert(db, AlertKind.BATCH_RELEASED, route_id=appealed.route_id)
    # 09-03: Walter stops for the day holding P3, Yuri does it.
    await _stop(
        db, walter, date(2026, 9, 3), p3, status=released, reason=ReleaseReason.ROUTE_REMAINDER
    )
    await _execution(
        db, await _stop(db, yuri, date(2026, 9, 3), p3, status=done), yuri, evidence=True
    )
    # 09-04: the day ends with P2 still in Xavier's batch; two day-level alerts, the second at
    # 22:00 on campus — already 09-05 in UTC.
    await _stop(db, xavier, date(2026, 9, 4), p2, status=released, reason=ReleaseReason.END_OF_DAY)
    _alert(db, AlertKind.POINTS_UNSERVED, created_at=_at(date(2026, 9, 4), 21))
    _alert(db, AlertKind.POINTS_UNSERVED, created_at=datetime(2026, 9, 5, 1, 0, tzinfo=UTC))
    await db.commit()

    floor = await db.get(Floor, p1.floor_id)
    assert floor is not None
    building = await db.get(Building, floor.building_id)
    assert building is not None
    return _Ids(
        walter=walter.id,
        yuri=yuri.id,
        cleaning_company=cleaning.contractor_company.id,
        cleaning_type=cleaning.id,
        building_1=building.id,
        building_1_name=building.name,
    )


def _metrics(**nonzero: int) -> dict[str, int]:
    return {name: nonzero.get(name, 0) for name in _METRICS}


# Hand-computed constants, one per service day -------------------------------------------------

_DAYS = {
    date(2026, 8, 27): _metrics(
        points_total=2,
        points_done=1,
        points_unserved=1,
        points_released=1,
        released_left_campus=1,
        executions_total=2,
        executions_with_evidence=1,
        alerts_total=1,
    ),
    date(2026, 8, 28): _metrics(
        points_total=2, points_unserved=2, points_released=1, points_impeded=1, alerts_total=1
    ),
    date(2026, 8, 31): _metrics(points_total=2, points_unserved=2),
    date(2026, 9, 1): _metrics(
        points_total=2,
        points_done=1,
        points_done_by_appeal=1,
        points_unserved=1,
        points_released=1,
        released_location_silence=1,
        executions_total=1,
        executions_flagged=1,
        alerts_total=1,
    ),
    date(2026, 9, 2): _metrics(points_total=3, points_unserved=3),
    date(2026, 9, 3): _metrics(
        points_total=3,
        points_done=1,
        points_unserved=2,
        points_released=1,
        released_route_remainder=1,
        executions_total=1,
        executions_with_evidence=1,
    ),
    date(2026, 9, 4): _metrics(
        points_total=3,
        points_unserved=3,
        points_released=1,
        released_end_of_day=1,
        alerts_total=2,
    ),
}


def _merge(*days: date) -> dict[str, int]:
    return {name: sum(_DAYS[day][name] for day in days) for name in _METRICS}


def _assert_row(row: ComplianceRow, label: str, start: date, expected: dict[str, int]) -> None:
    assert (row.period_label, row.period_start) == (label, start)
    assert {name: getattr(row, name) for name in _METRICS} == expected
    assert row.completion_rate == pytest.approx(expected["points_done"] / expected["points_total"])


def _totals(report: ComplianceReport) -> dict[str, int]:
    return {name: getattr(report.totals, name) for name in _METRICS}


async def _report(
    db: AsyncSession, granularity: ReportGranularity, filters: ReportFilters = _RANGE
) -> ComplianceReport:
    return await build_compliance_report(db, granularity=granularity, filters=filters)


async def test_daily_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(db_session, ReportGranularity.DAILY)

    assert len(report.periods) == len(_DAYS)
    for row, (day, expected) in zip(report.periods, _DAYS.items(), strict=True):
        _assert_row(row, day.isoformat(), day, expected)
    totals = _totals(report)
    assert totals == _merge(*_DAYS)
    assert totals["points_total"] == 17
    assert totals["alerts_total"] == 5
    assert report.totals.completion_rate == pytest.approx(3 / 17)


async def test_weekly_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(db_session, ReportGranularity.WEEKLY)

    week_35, week_36 = report.periods
    _assert_row(
        week_35, "2026-W35", date(2026, 8, 24), _merge(date(2026, 8, 27), date(2026, 8, 28))
    )
    _assert_row(
        week_36,
        "2026-W36",
        date(2026, 8, 31),
        _merge(*(day for day in _DAYS if day >= date(2026, 8, 31))),
    )


async def test_monthly_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(db_session, ReportGranularity.MONTHLY)

    august, september = report.periods
    _assert_row(august, "2026-08", date(2026, 8, 1), _merge(*(d for d in _DAYS if d.month == 8)))
    _assert_row(september, "2026-09", date(2026, 9, 1), _merge(*(d for d in _DAYS if d.month == 9)))


async def test_days_without_typed_points_are_absent(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await build_compliance_report(
        db_session,
        granularity=ReportGranularity.DAILY,
        filters=ReportFilters(date_from=date(2025, 12, 1), date_to=date(2025, 12, 31)),
    )

    assert report.periods == []
    assert report.totals.points_total == 0
    assert report.totals.completion_rate == 0.0


async def test_company_filter_narrows_to_its_points(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)

    report = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, company_id=ids.cleaning_company)
    )

    # P1 on seven days, P3 on three; their executions and the NO_EVIDENCE on P1, plus the batch
    # alert of Walter's route — Walter works for the company. The day-level alerts have no
    # company and stay out.
    assert _totals(report) == _metrics(
        points_total=10,
        points_done=3,
        points_done_by_appeal=1,
        points_unserved=7,
        points_released=3,
        released_left_campus=1,
        released_location_silence=1,
        released_route_remainder=1,
        executions_total=4,
        executions_with_evidence=2,
        executions_flagged=1,
        alerts_total=2,
    )
    assert report.filters_applied.company_name == "Limpa Tudo"


async def test_service_type_and_building_filters(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)

    by_type = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, service_type_id=ids.cleaning_type)
    )
    by_building = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, building_id=ids.building_1)
    )

    assert by_type.totals == by_building.totals
    assert by_type.totals.points_total == 10
    # Only the alert on P1's stop links to a type and a building; the route and day alerts don't.
    assert by_type.totals.alerts_total == 1
    assert by_type.totals.executions_total == 4
    assert by_type.filters_applied.service_type_name == "Limpeza"
    assert by_building.filters_applied.building_name == ids.building_1_name


async def test_worker_filter_credits_only_their_own_work(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)

    walter = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, field_worker_id=ids.walter)
    )
    yuri = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, field_worker_id=ids.yuri)
    )

    # Walter held P1 on 08-27 (did it), P1 on 09-01 (won the appeal) and P3 on 09-03 (released
    # it, Yuri did it — no outcome for Walter). Yuri's 08-27 release and his NO_EVIDENCE alert
    # are not Walter's; the batch alert of Walter's 09-01 route is.
    assert _totals(walter) == _metrics(
        points_total=3,
        points_done=2,
        points_done_by_appeal=1,
        points_released=2,
        released_location_silence=1,
        released_route_remainder=1,
        executions_total=2,
        executions_with_evidence=1,
        executions_flagged=1,
        alerts_total=1,
    )
    # Yuri held P1 on 08-27 (left the campus, Walter did it — no outcome) and P3 on 09-03 (did
    # it after Walter's release, which is not Yuri's).
    assert _totals(yuri) == _metrics(
        points_total=2,
        points_done=1,
        points_released=1,
        released_left_campus=1,
        executions_total=2,
        executions_with_evidence=1,
        alerts_total=1,
    )
    assert walter.filters_applied.field_worker_name == "Walter"
    assert walter.filters_applied.company_name is None


async def test_outcome_filter_done(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(db_session, ReportGranularity.MONTHLY, replace(_RANGE, outcome="DONE"))

    # P1 on 08-27 and P3 on 09-03, with their executions and P1's stop alert.
    assert _totals(report) == _metrics(
        points_total=2,
        points_done=2,
        points_released=2,
        released_left_campus=1,
        released_route_remainder=1,
        executions_total=3,
        executions_with_evidence=2,
        alerts_total=1,
    )
    assert report.filters_applied.outcome == "Feito"


async def test_outcome_filter_done_by_appeal(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(
        db_session, ReportGranularity.MONTHLY, replace(_RANGE, outcome="DONE_BY_APPEAL")
    )

    assert _totals(report) == _metrics(
        points_total=1,
        points_done=1,
        points_done_by_appeal=1,
        points_released=1,
        released_location_silence=1,
        executions_total=1,
        executions_flagged=1,
    )
    assert report.filters_applied.outcome == "Feito por recurso"


async def test_outcome_filter_unserved(db_session: AsyncSession) -> None:
    await _seed(db_session)

    report = await _report(db_session, ReportGranularity.DAILY, replace(_RANGE, outcome="UNSERVED"))

    assert _totals(report) == _metrics(
        points_total=14,
        points_unserved=14,
        points_released=2,
        points_impeded=1,
        released_end_of_day=1,
        alerts_total=1,
    )
    assert [row.points_unserved for row in report.periods] == [
        day["points_unserved"] for day in _DAYS.values()
    ]
    assert report.filters_applied.outcome == "Não atendido"


async def test_outcome_filter_applies_to_the_workers_credit(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)

    report = await _report(
        db_session,
        ReportGranularity.MONTHLY,
        replace(_RANGE, field_worker_id=ids.walter, outcome="DONE"),
    )

    # Only 08-27: the 09-03 point-day was done, but by Yuri.
    assert report.totals.points_total == 1
    assert report.totals.points_done == 1


def test_outcome_labels_cover_every_outcome() -> None:
    outcomes: tuple[PointDayOutcome, ...] = get_args(PointDayOutcome)
    assert set(OUTCOME_LABELS) == set(outcomes)
