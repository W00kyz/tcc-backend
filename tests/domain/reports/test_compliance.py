"""RF45 — `build_compliance_report` against the real testcontainer.

A single fixed scenario of 4 routes spanning two ISO weeks across the Aug/Sep 2026 boundary, so
DAILY, WEEKLY and MONTHLY each produce more than one bucket. Every one of the 13 indicators plus
`completion_rate` is asserted per bucket and in the totals row against hand-computed constants,
for each granularity, plus the filter-narrowing and edge cases.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    ManualCompletion,
)
from app.domain.identity.models import User, UserRole
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
)
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.reports.compliance import (
    ComplianceRow,
    ReportGranularity,
    build_compliance_report,
)
from app.domain.reports.filters import ReportFilters
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    StopAssignment,
)
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_METRICS = (
    "routes_total",
    "routes_started",
    "routes_completed",
    "routes_cancelled",
    "stops_total",
    "stops_done",
    "stops_pending",
    "stops_reassigned",
    "executions_total",
    "executions_with_evidence",
    "executions_flagged",
    "impediments_total",
    "alerts_total",
)


class _Ids:
    def __init__(self) -> None:
        self.worker: uuid.UUID
        self.building_1: uuid.UUID
        self.building_2: uuid.UUID
        self.service_type_1: uuid.UUID
        self.service_type_2: uuid.UUID


async def _seed(db: AsyncSession) -> _Ids:
    ids = _Ids()
    user = User(
        name="Gestor",
        email=f"gestor-{uuid.uuid4()}@ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj=str(uuid.uuid4().int)[:14])
    b1 = Building(name="Bloco CI", campus_area="CCT")
    b2 = Building(name="Bloco CN", campus_area="CCT")
    s1 = ServiceType(name="Varrição", average_duration_minutes=30)
    s2 = ServiceType(name="Coleta", average_duration_minutes=20)
    db.add_all([user, company, b1, b2, s1, s2])
    await db.flush()
    f1 = Floor(building_id=b1.id, label="Térreo")
    f2 = Floor(building_id=b2.id, label="Térreo")
    db.add_all([f1, f2])
    await db.flush()
    p1 = ServicePoint(
        floor_id=f1.id, name="Sala 101", description="x", latitude=-7.2, longitude=-35.9
    )
    p2 = ServicePoint(
        floor_id=f2.id, name="Sala 201", description="x", latitude=-7.2, longitude=-35.9
    )
    worker = FieldWorker(full_name="João da Silva", contractor_company_id=company.id)
    db.add_all([p1, p2, worker])
    await db.flush()

    ids.worker = worker.id
    ids.building_1 = b1.id
    ids.building_2 = b2.id
    ids.service_type_1 = s1.id
    ids.service_type_2 = s2.id

    def _clock(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)

    async def _stop(
        route: Route,
        point_id: uuid.UUID,
        service_type_id: uuid.UUID,
        status: RouteStopStatus,
        order_index: int,
    ) -> RouteStop:
        stop = RouteStop(
            route_id=route.id,
            service_point_id=point_id,
            service_type_id=service_type_id,
            order_index=order_index,
            status=status,
        )
        db.add(stop)
        await db.flush()
        return stop

    async def _execution(
        stop: RouteStop, day: date, review_status: ExecutionReviewStatus
    ) -> Execution:
        execution = Execution(
            route_stop_id=stop.id,
            field_worker_id=worker.id,
            checked_in_at=_clock(day),
            synced_at=_clock(day),
            source=ExecutionSource.APP,
            idempotency_key=uuid.uuid4(),
            review_status=review_status,
        )
        db.add(execution)
        await db.flush()
        return execution

    # R1 — 2026-08-27 (Thu), DONE, started.
    r1 = Route(
        field_worker_id=worker.id,
        route_date=date(2026, 8, 27),
        status=RouteStatus.DONE,
        started_at=_clock(date(2026, 8, 27)),
    )
    db.add(r1)
    await db.flush()
    r1_s1 = await _stop(r1, p1.id, s1.id, RouteStopStatus.DONE, 0)
    await _stop(r1, p2.id, s2.id, RouteStopStatus.PENDING, 1)
    e1 = await _execution(r1_s1, date(2026, 8, 27), ExecutionReviewStatus.NONE)
    db.add(
        EvidenceItem(
            execution_id=e1.id,
            kind=EvidenceKind.NOTE,
            text_body="feito",
            captured_at=_clock(date(2026, 8, 27)),
        )
    )
    await _execution(r1_s1, date(2026, 8, 27), ExecutionReviewStatus.PENDING_REVIEW)
    db.add(
        Impediment(
            route_id=r1.id,
            scope=ImpedimentScope.ROUTE_REMAINDER,
            reason=ImpedimentReason.OTHER,
            reported_by=worker.id,
            reported_at=_clock(date(2026, 8, 27)),
        )
    )
    db.add(
        Alert(
            kind=AlertKind.LATE_STOP,
            severity=AlertSeverity.WARNING,
            dedup_key=f"k-{uuid.uuid4()}",
            description="atraso",
            route_id=r1.id,
        )
    )

    # R2 — 2026-08-28 (Fri), CANCELLED, not started. One PENDING stop in building 2 / type 1.
    r2 = Route(
        field_worker_id=worker.id,
        route_date=date(2026, 8, 28),
        status=RouteStatus.CANCELLED,
    )
    db.add(r2)
    await db.flush()
    await _stop(r2, p2.id, s1.id, RouteStopStatus.PENDING, 0)

    # R3 — 2026-09-03 (Thu), PLANNED, no stops → routes but zero stops.
    r3 = Route(
        field_worker_id=worker.id,
        route_date=date(2026, 9, 3),
        status=RouteStatus.PLANNED,
    )
    db.add(r3)
    await db.flush()

    # R4 — 2026-09-04 (Fri), IN_PROGRESS, not started (status alone counts as started).
    r4 = Route(
        field_worker_id=worker.id,
        route_date=date(2026, 9, 4),
        status=RouteStatus.IN_PROGRESS,
    )
    db.add(r4)
    await db.flush()
    r4_s1 = await _stop(r4, p2.id, s2.id, RouteStopStatus.DONE, 0)
    await _stop(r4, p1.id, s1.id, RouteStopStatus.IN_PROGRESS, 1)
    db.add_all(
        [
            StopAssignment(
                route_stop_id=r4_s1.id, field_worker_id=worker.id, sequence=1, assigned_by=user.id
            ),
            StopAssignment(
                route_stop_id=r4_s1.id, field_worker_id=worker.id, sequence=2, assigned_by=user.id
            ),
        ]
    )
    e3 = await _execution(r4_s1, date(2026, 9, 4), ExecutionReviewStatus.NONE)
    db.add(
        ManualCompletion(
            route_stop_id=r4_s1.id,
            execution_id=e3.id,
            completed_by=user.id,
            reason="offline",
            completed_at=_clock(date(2026, 9, 4)),
        )
    )
    db.add(
        Alert(
            kind=AlertKind.LATE_STOP,
            severity=AlertSeverity.WARNING,
            dedup_key=f"k-{uuid.uuid4()}",
            description="atraso",
            route_id=r4.id,
        )
    )
    await db.commit()
    return ids


def _assert_row(
    row: ComplianceRow, period_label: str, period_start: date, **expected: object
) -> None:
    assert row.period_label == period_label
    assert row.period_start == period_start
    for name, value in expected.items():
        assert getattr(row, name) == value, name


def _full_range() -> ReportFilters:
    return ReportFilters(date_from=date(2026, 8, 1), date_to=date(2026, 9, 30))


# Hand-computed constants ----------------------------------------------------------------------

_D1 = dict(  # noqa: C408 - a literal metric table reads better as kwargs
    routes_total=1,
    routes_started=1,
    routes_completed=1,
    routes_cancelled=0,
    stops_total=2,
    stops_done=1,
    stops_pending=1,
    stops_reassigned=0,
    executions_total=2,
    executions_with_evidence=1,
    executions_flagged=1,
    impediments_total=1,
    alerts_total=1,
)
_D2 = dict(  # noqa: C408
    routes_total=1,
    routes_started=0,
    routes_completed=0,
    routes_cancelled=1,
    stops_total=1,
    stops_done=0,
    stops_pending=1,
    stops_reassigned=0,
    executions_total=0,
    executions_with_evidence=0,
    executions_flagged=0,
    impediments_total=0,
    alerts_total=0,
)
_D3 = dict(  # noqa: C408
    routes_total=1,
    routes_started=0,
    routes_completed=0,
    routes_cancelled=0,
    stops_total=0,
    stops_done=0,
    stops_pending=0,
    stops_reassigned=0,
    executions_total=0,
    executions_with_evidence=0,
    executions_flagged=0,
    impediments_total=0,
    alerts_total=0,
)
_D4 = dict(  # noqa: C408
    routes_total=1,
    routes_started=1,
    routes_completed=0,
    routes_cancelled=0,
    stops_total=2,
    stops_done=1,
    stops_pending=0,
    stops_reassigned=1,
    executions_total=1,
    executions_with_evidence=1,
    executions_flagged=0,
    impediments_total=0,
    alerts_total=1,
)


def _merge(*rows: dict[str, int]) -> dict[str, int]:
    return {name: sum(row[name] for row in rows) for name in _METRICS}


async def test_daily_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=_full_range()
    )
    assert [r.period_start for r in report.periods] == [
        date(2026, 8, 27),
        date(2026, 8, 28),
        date(2026, 9, 3),
        date(2026, 9, 4),
    ]
    _assert_row(report.periods[0], "2026-08-27", date(2026, 8, 27), **_D1, completion_rate=0.5)
    _assert_row(report.periods[1], "2026-08-28", date(2026, 8, 28), **_D2, completion_rate=0.0)
    _assert_row(report.periods[2], "2026-09-03", date(2026, 9, 3), **_D3, completion_rate=0.0)
    _assert_row(report.periods[3], "2026-09-04", date(2026, 9, 4), **_D4, completion_rate=0.5)

    totals = _merge(_D1, _D2, _D3, _D4)
    for name in _METRICS:
        assert getattr(report.totals, name) == totals[name], name
    assert report.totals.completion_rate == pytest.approx(2 / 5)


async def test_weekly_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.WEEKLY, filters=_full_range()
    )
    assert [r.period_start for r in report.periods] == [date(2026, 8, 24), date(2026, 8, 31)]
    _assert_row(
        report.periods[0],
        "2026-W35",
        date(2026, 8, 24),
        **_merge(_D1, _D2),
        completion_rate=pytest.approx(1 / 3),
    )
    _assert_row(
        report.periods[1],
        "2026-W36",
        date(2026, 8, 31),
        **_merge(_D3, _D4),
        completion_rate=0.5,
    )
    assert report.totals.completion_rate == pytest.approx(2 / 5)
    assert report.totals.stops_reassigned == 1


async def test_monthly_metrics(db_session: AsyncSession) -> None:
    await _seed(db_session)
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.MONTHLY, filters=_full_range()
    )
    assert [r.period_start for r in report.periods] == [date(2026, 8, 1), date(2026, 9, 1)]
    _assert_row(
        report.periods[0],
        "2026-08",
        date(2026, 8, 1),
        **_merge(_D1, _D2),
        completion_rate=pytest.approx(1 / 3),
    )
    _assert_row(
        report.periods[1],
        "2026-09",
        date(2026, 9, 1),
        **_merge(_D3, _D4),
        completion_rate=0.5,
    )


async def test_completion_rate_zero_for_period_with_routes_but_no_stops(
    db_session: AsyncSession,
) -> None:
    await _seed(db_session)
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=_full_range()
    )
    sep_3 = next(r for r in report.periods if r.period_start == date(2026, 9, 3))
    assert sep_3.routes_total == 1
    assert sep_3.stops_total == 0
    assert sep_3.completion_rate == 0.0


async def test_empty_periods_are_absent(db_session: AsyncSession) -> None:
    await _seed(db_session)
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=_full_range()
    )
    days = {r.period_start for r in report.periods}
    assert date(2026, 8, 29) not in days
    assert date(2026, 9, 1) not in days
    assert len(report.periods) == 4


async def test_service_type_filter_narrows_scope(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)
    filters = ReportFilters(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        service_type_id=ids.service_type_2,
    )
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=filters
    )
    # Only R1 (08-27) and R4 (09-04) have a stop of type 2 — R2/R3 drop out entirely.
    assert [r.period_start for r in report.periods] == [date(2026, 8, 27), date(2026, 9, 4)]
    _assert_row(report.periods[0], "2026-08-27", date(2026, 8, 27), **_D1, completion_rate=0.5)
    _assert_row(report.periods[1], "2026-09-04", date(2026, 9, 4), **_D4, completion_rate=0.5)
    assert report.filters_applied.service_type_name == "Coleta"


async def test_building_filter_narrows_scope(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)
    filters = ReportFilters(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        building_id=ids.building_1,
    )
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=filters
    )
    # Building 1 is only touched by R1 (stop 1) and R4 (stop 2).
    assert [r.period_start for r in report.periods] == [date(2026, 8, 27), date(2026, 9, 4)]
    assert report.filters_applied.building_name == "Bloco CI"


async def test_route_status_cancelled_filter(db_session: AsyncSession) -> None:
    await _seed(db_session)
    filters = ReportFilters(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        route_status=RouteStatus.CANCELLED.value,
    )
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.DAILY, filters=filters
    )
    assert [r.period_start for r in report.periods] == [date(2026, 8, 28)]
    _assert_row(report.periods[0], "2026-08-28", date(2026, 8, 28), **_D2, completion_rate=0.0)
    assert report.filters_applied.route_status == "CANCELLED"


async def test_filters_applied_resolves_worker_name(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)
    filters = ReportFilters(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        field_worker_id=ids.worker,
    )
    report = await build_compliance_report(
        db_session, granularity=ReportGranularity.MONTHLY, filters=filters
    )
    assert report.filters_applied.field_worker_name == "João da Silva"
    assert report.filters_applied.service_type_name is None
