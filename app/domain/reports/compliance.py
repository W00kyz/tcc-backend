"""RF45 — the route-compliance read model (spec §2 Rulings 3, 4, 5, 8).

`build_compliance_report` groups the routes whose `route_date` falls in the filter range (and
that pass the optional filters) into daily / weekly / monthly buckets and computes the fixed set
of 13 compliance indicators plus `completion_rate` for each bucket, plus a totals row.

Strategy: five `GROUP BY <period bucket>` queries — route counts, stop counts, execution counts,
impediment counts, alert counts — each scoped to the same set of routes, then zipped by
`period_start` in Python. Impediments and alerts are bucketed by their route's `route_date`
(spec §2 Ruling 3), not their own timestamp, so a period's row is "everything about the routes
planned that period". Only buckets with `routes_total >= 1` are returned (spec §2 Ruling 4).

The builder assumes `filters.date_from <= filters.date_to`; the endpoint validates that and caps
the span (spec §2 Ruling 5).

Read-only: no `flush`, no `commit`.

Example:
    report = await build_compliance_report(
        db, granularity=ReportGranularity.MONTHLY, filters=filters
    )
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, Date, Row, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.catalog.models import Building, FieldWorker, Floor, ServicePoint, ServiceType
from app.domain.execution.models import (
    EvidenceItem,
    Execution,
    ExecutionReviewStatus,
    ManualCompletion,
)
from app.domain.impediments.models import Impediment
from app.domain.monitoring.models import Alert
from app.domain.reports.filters import ReportFilters
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus, StopAssignment


class ReportGranularity(enum.StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


# The pt-BR label for each route status — `filters_applied` is the human-readable struct
# (spec §2 Ruling 8), so `route_status` carries the display label, not the raw enum value.
# These strings must match the dashboard i18n (`routeStatus*` / `reportsRouteStatus*`).
_ROUTE_STATUS_LABELS: dict[RouteStatus, str] = {
    RouteStatus.PLANNED: "Planejada",
    RouteStatus.IN_PROGRESS: "Em andamento",
    RouteStatus.CANCELLED: "Cancelada",
    RouteStatus.DONE: "Concluída",
}


# The 13 absolute indicators, in report order. Kept as a tuple so the totals row and the
# zero-fill both iterate the exact same set instead of restating 13 field names.
_METRIC_FIELDS: tuple[str, ...] = (
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


@dataclass(frozen=True)
class ComplianceRow:
    period_label: str  # "2026-09-07" | "2026-W37" | "2026-09"
    period_start: date
    routes_total: int
    routes_started: int
    routes_completed: int
    routes_cancelled: int
    stops_total: int
    stops_done: int
    stops_pending: int
    stops_reassigned: int
    executions_total: int
    executions_with_evidence: int
    executions_flagged: int
    impediments_total: int
    alerts_total: int
    completion_rate: float  # stops_done / stops_total, 0.0 when stops_total == 0


@dataclass(frozen=True)
class ComplianceTotals:
    routes_total: int
    routes_started: int
    routes_completed: int
    routes_cancelled: int
    stops_total: int
    stops_done: int
    stops_pending: int
    stops_reassigned: int
    executions_total: int
    executions_with_evidence: int
    executions_flagged: int
    impediments_total: int
    alerts_total: int
    completion_rate: float


@dataclass(frozen=True)
class FilterApplied:
    field_worker_name: str | None
    service_type_name: str | None
    building_name: str | None
    route_status: str | None


@dataclass(frozen=True)
class ComplianceReport:
    granularity: ReportGranularity
    date_from: date
    date_to: date
    filters_applied: FilterApplied
    periods: list[ComplianceRow]
    totals: ComplianceTotals
    generated_at: datetime


def _completion_rate(stops_done: int, stops_total: int) -> float:
    return stops_done / stops_total if stops_total else 0.0


def _period_expr(granularity: ReportGranularity) -> ColumnElement[date]:
    """The SQL expression that maps `route_date` to its bucket start (a `date`)."""
    if granularity is ReportGranularity.DAILY:
        return cast(Route.route_date, Date)
    unit = "week" if granularity is ReportGranularity.WEEKLY else "month"
    # Postgres `date_trunc('week', ...)` anchors on Monday — the ISO week start.
    return cast(func.date_trunc(unit, Route.route_date), Date)


def _period_label(granularity: ReportGranularity, period_start: date) -> str:
    if granularity is ReportGranularity.DAILY:
        return period_start.isoformat()
    if granularity is ReportGranularity.WEEKLY:
        iso = period_start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    return f"{period_start.year:04d}-{period_start.month:02d}"


def _scope_conditions(filters: ReportFilters) -> list[ColumnElement[bool]]:
    """The WHERE that defines "routes in scope" — a range on `route_date` plus the optional
    filters. `building_id` / `service_type_id` use EXISTS over `route_stops` so a route with
    several matching stops is still counted once (spec §2 Ruling 4)."""
    conditions: list[ColumnElement[bool]] = [
        Route.route_date >= filters.date_from,
        Route.route_date <= filters.date_to,
    ]
    if filters.field_worker_id is not None:
        conditions.append(Route.field_worker_id == filters.field_worker_id)
    if filters.route_status is not None:
        conditions.append(Route.status == RouteStatus(filters.route_status))
    if filters.service_type_id is not None:
        # An aliased RouteStop + explicit correlate(Route): the outer aggregate queries also
        # select from `route_stops`, and without this SQLAlchemy auto-correlates the subquery's
        # own FROM away ("returned no FROM clauses due to auto-correlation").
        scoped_stop = aliased(RouteStop)
        conditions.append(
            select(scoped_stop.id)
            .where(
                scoped_stop.route_id == Route.id,
                scoped_stop.service_type_id == filters.service_type_id,
            )
            .correlate(Route)
            .exists()
        )
    if filters.building_id is not None:
        scoped_stop = aliased(RouteStop)
        conditions.append(
            select(scoped_stop.id)
            .join(ServicePoint, scoped_stop.service_point_id == ServicePoint.id)
            .join(Floor, ServicePoint.floor_id == Floor.id)
            .where(scoped_stop.route_id == Route.id, Floor.building_id == filters.building_id)
            .correlate(Route)
            .exists()
        )
    return conditions


async def _route_counts(
    db: AsyncSession, period: ColumnElement[date], filters: ReportFilters
) -> dict[date, Row[tuple[date, int, int, int, int]]]:
    started = or_(
        Route.started_at.is_not(None),
        Route.status.in_([RouteStatus.IN_PROGRESS, RouteStatus.DONE]),
    )
    stmt = (
        select(
            period.label("period_start"),
            func.count().label("routes_total"),
            func.count().filter(started).label("routes_started"),
            func.count().filter(Route.status == RouteStatus.DONE).label("routes_completed"),
            func.count().filter(Route.status == RouteStatus.CANCELLED).label("routes_cancelled"),
        )
        .where(*_scope_conditions(filters))
        .group_by(period)
    )
    return {row.period_start: row for row in (await db.execute(stmt)).all()}


async def _stop_counts(
    db: AsyncSession, period: ColumnElement[date], filters: ReportFilters
) -> dict[date, Row[tuple[date, int, int, int, int]]]:
    reassigned = (
        select(func.count(StopAssignment.id))
        .where(StopAssignment.route_stop_id == RouteStop.id)
        .scalar_subquery()
    ) >= 2
    stmt = (
        select(
            period.label("period_start"),
            func.count(RouteStop.id).label("stops_total"),
            func.count().filter(RouteStop.status == RouteStopStatus.DONE).label("stops_done"),
            func.count().filter(RouteStop.status == RouteStopStatus.PENDING).label("stops_pending"),
            func.count().filter(reassigned).label("stops_reassigned"),
        )
        .select_from(RouteStop)
        .join(Route, RouteStop.route_id == Route.id)
        .where(*_scope_conditions(filters))
        .group_by(period)
    )
    return {row.period_start: row for row in (await db.execute(stmt)).all()}


async def _execution_counts(
    db: AsyncSession, period: ColumnElement[date], filters: ReportFilters
) -> dict[date, Row[tuple[date, int, int, int]]]:
    has_evidence = or_(
        select(EvidenceItem.id).where(EvidenceItem.execution_id == Execution.id).exists(),
        select(ManualCompletion.id).where(ManualCompletion.execution_id == Execution.id).exists(),
    )
    flagged = Execution.review_status == ExecutionReviewStatus.PENDING_REVIEW
    stmt = (
        select(
            period.label("period_start"),
            func.count(Execution.id).label("executions_total"),
            func.count().filter(has_evidence).label("executions_with_evidence"),
            func.count().filter(flagged).label("executions_flagged"),
        )
        .select_from(Execution)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .join(Route, RouteStop.route_id == Route.id)
        .where(*_scope_conditions(filters))
        .group_by(period)
    )
    return {row.period_start: row for row in (await db.execute(stmt)).all()}


async def _related_counts(
    db: AsyncSession,
    period: ColumnElement[date],
    filters: ReportFilters,
    *,
    model: type[Impediment] | type[Alert],
) -> dict[date, int]:
    """Impediments or alerts, joined to their route and bucketed by `route.route_date`."""
    stmt = (
        select(period.label("period_start"), func.count(model.id).label("n"))
        .select_from(model)
        .join(Route, model.route_id == Route.id)
        .where(*_scope_conditions(filters))
        .group_by(period)
    )
    return {row.period_start: int(row.n) for row in (await db.execute(stmt)).all()}


async def _resolve_filters_applied(db: AsyncSession, filters: ReportFilters) -> FilterApplied:
    """One point lookup per present UUID filter → its display name (spec §2 Ruling 8)."""
    worker_name: str | None = None
    service_type_name: str | None = None
    building_name: str | None = None
    if filters.field_worker_id is not None:
        worker = await db.get(FieldWorker, filters.field_worker_id)
        worker_name = worker.full_name if worker is not None else None
    if filters.service_type_id is not None:
        service_type = await db.get(ServiceType, filters.service_type_id)
        service_type_name = service_type.name if service_type is not None else None
    if filters.building_id is not None:
        building = await db.get(Building, filters.building_id)
        building_name = building.name if building is not None else None
    status_label: str | None = None
    if filters.route_status is not None:
        status_label = _ROUTE_STATUS_LABELS[RouteStatus(filters.route_status)]
    return FilterApplied(
        field_worker_name=worker_name,
        service_type_name=service_type_name,
        building_name=building_name,
        route_status=status_label,
    )


def _build_row(
    granularity: ReportGranularity,
    period_start: date,
    routes: Row[tuple[date, int, int, int, int]],
    stops: Row[tuple[date, int, int, int, int]] | None,
    executions: Row[tuple[date, int, int, int]] | None,
    impediments_total: int,
    alerts_total: int,
) -> ComplianceRow:
    stops_total = stops.stops_total if stops is not None else 0
    stops_done = stops.stops_done if stops is not None else 0
    return ComplianceRow(
        period_label=_period_label(granularity, period_start),
        period_start=period_start,
        routes_total=routes.routes_total,
        routes_started=routes.routes_started,
        routes_completed=routes.routes_completed,
        routes_cancelled=routes.routes_cancelled,
        stops_total=stops_total,
        stops_done=stops_done,
        stops_pending=stops.stops_pending if stops is not None else 0,
        stops_reassigned=stops.stops_reassigned if stops is not None else 0,
        executions_total=executions.executions_total if executions is not None else 0,
        executions_with_evidence=(
            executions.executions_with_evidence if executions is not None else 0
        ),
        executions_flagged=executions.executions_flagged if executions is not None else 0,
        impediments_total=impediments_total,
        alerts_total=alerts_total,
        completion_rate=_completion_rate(stops_done, stops_total),
    )


def _build_totals(rows: Sequence[ComplianceRow]) -> ComplianceTotals:
    sums = {name: sum(getattr(row, name) for row in rows) for name in _METRIC_FIELDS}
    return ComplianceTotals(
        completion_rate=_completion_rate(sums["stops_done"], sums["stops_total"]),
        **{f.name: sums[f.name] for f in fields(ComplianceTotals) if f.name != "completion_rate"},
    )


async def build_compliance_report(
    db: AsyncSession, *, granularity: ReportGranularity, filters: ReportFilters
) -> ComplianceReport:
    """Aggregate the scoped routes into `granularity` buckets with the 13 compliance indicators
    plus `completion_rate` and a totals row. Assumes `filters.date_from <= filters.date_to`."""
    period = _period_expr(granularity)
    route_counts = await _route_counts(db, period, filters)
    stop_counts = await _stop_counts(db, period, filters)
    execution_counts = await _execution_counts(db, period, filters)
    impediment_counts = await _related_counts(db, period, filters, model=Impediment)
    alert_counts = await _related_counts(db, period, filters, model=Alert)

    # `route_counts` holds exactly the buckets with routes_total >= 1 — the only ones that appear
    # (spec §2 Ruling 4). Sorted ascending by period start.
    periods = [
        _build_row(
            granularity,
            period_start,
            route_counts[period_start],
            stop_counts.get(period_start),
            execution_counts.get(period_start),
            impediment_counts.get(period_start, 0),
            alert_counts.get(period_start, 0),
        )
        for period_start in sorted(route_counts)
    ]

    return ComplianceReport(
        granularity=granularity,
        date_from=filters.date_from,
        date_to=filters.date_to,
        filters_applied=await _resolve_filters_applied(db, filters),
        periods=periods,
        totals=_build_totals(periods),
        generated_at=datetime.now(UTC),
    )
