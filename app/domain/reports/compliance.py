"""RF45 — the service-compliance read model (spec §2 Rulings 4, 5, 8; spec
2026-10-20-alocacao-automatica §8.7).

`build_compliance_report` counts the point-days of `point_day_rows` — every typed point on every
service day it existed, counted once — into daily / weekly / monthly buckets: how many were done
(by a stop or by an accepted appeal), how many went unserved, how many were released, by reason,
and the executions and alerts linked to them (`activity`). Only buckets with something to count
are returned (spec §2 Ruling 4), so weekends never show.

Narrowed to one worker, each point-day they held is credited with their own work only
(`PointDay.outcome_for`): a point-day another worker served is in `points_total` but in no
outcome counter, so done + unserved may fall short of the total.

The builder assumes `filters.date_from <= filters.date_to`; the endpoint validates that and caps
the span (spec §2 Ruling 5).

Read-only: no `commit`.

Example:
    report = await build_compliance_report(
        db, granularity=ReportGranularity.MONTHLY, filters=filters
    )
"""

import enum
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, ServiceType
from app.domain.reports.activity import (
    ExecutionCounts,
    executions_by_point_day,
    stop_alerts_by_point_day,
    unlinked_alerts_by_day,
)
from app.domain.reports.filters import ReportFilters
from app.domain.reports.point_days import PointDay, PointDayOutcome, point_day_rows
from app.domain.routing.models import ReleaseReason


class ReportGranularity(enum.StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


# The pt-BR label of each outcome — `filters_applied` is the human-readable struct (spec §2
# Ruling 8). These strings must match the dashboard i18n of the outcome filter.
OUTCOME_LABELS: dict[PointDayOutcome, str] = {
    "DONE": "Feito",
    "DONE_BY_APPEAL": "Feito por recurso",
    "UNSERVED": "Não atendido",
}
_NO_EXECUTIONS = ExecutionCounts(total=0, with_evidence=0, flagged=0)


@dataclass(frozen=True)
class ComplianceCounts:
    """The indicators of a bucket. The `points_*` / `released_*` ones count point-days:
    `points_done` includes the ones done by appeal, `points_impeded` is the IMPEDIMENT release
    reason; a point-day released twice for the same reason counts once in that reason's
    counter, released for two reasons, in both. `executions_*` and `alerts_total` count rows."""

    points_total: int
    points_done: int
    points_done_by_appeal: int
    points_unserved: int
    points_released: int
    points_impeded: int
    released_route_remainder: int
    released_left_campus: int
    released_location_silence: int
    released_end_of_day: int
    executions_total: int
    executions_with_evidence: int
    executions_flagged: int
    alerts_total: int

    @property
    def completion_rate(self) -> float:
        """points_done / points_total, 0.0 for an empty bucket."""
        return self.points_done / self.points_total if self.points_total else 0.0


@dataclass(frozen=True)
class ComplianceRow(ComplianceCounts):
    period_label: str  # "2026-09-07" | "2026-W37" | "2026-09"
    period_start: date


@dataclass(frozen=True)
class FilterApplied:
    field_worker_name: str | None
    company_name: str | None
    service_type_name: str | None
    building_name: str | None
    outcome: str | None  # the pt-BR label


@dataclass(frozen=True)
class _Tally:
    """One point-day as the report counts it — narrowed to the filtered worker, if any."""

    day: date
    outcome: PointDayOutcome | None  # None: another worker served it
    release_reasons: tuple[ReleaseReason, ...]
    executions: ExecutionCounts
    alerts: int


@dataclass(frozen=True)
class ComplianceReport:
    granularity: ReportGranularity
    date_from: date
    date_to: date
    filters_applied: FilterApplied
    periods: list[ComplianceRow]
    totals: ComplianceCounts
    generated_at: datetime


def _period_start(granularity: ReportGranularity, day: date) -> date:
    if granularity is ReportGranularity.DAILY:
        return day
    if granularity is ReportGranularity.WEEKLY:
        return day - timedelta(days=day.weekday())  # the ISO week starts on Monday
    return day.replace(day=1)


def _period_label(granularity: ReportGranularity, period_start: date) -> str:
    if granularity is ReportGranularity.DAILY:
        return period_start.isoformat()
    if granularity is ReportGranularity.WEEKLY:
        iso = period_start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    return f"{period_start.year:04d}-{period_start.month:02d}"


def _counts(tallies: Sequence[_Tally], unlinked_alerts: int) -> ComplianceCounts:
    def released(reason: ReleaseReason) -> int:
        return sum(1 for tally in tallies if reason in tally.release_reasons)

    def with_outcome(*outcomes: PointDayOutcome) -> int:
        return sum(1 for tally in tallies if tally.outcome in outcomes)

    return ComplianceCounts(
        points_total=len(tallies),
        points_done=with_outcome("DONE", "DONE_BY_APPEAL"),
        points_done_by_appeal=with_outcome("DONE_BY_APPEAL"),
        points_unserved=with_outcome("UNSERVED"),
        points_released=sum(1 for tally in tallies if tally.release_reasons),
        points_impeded=released(ReleaseReason.IMPEDIMENT),
        released_route_remainder=released(ReleaseReason.ROUTE_REMAINDER),
        released_left_campus=released(ReleaseReason.LEFT_CAMPUS),
        released_location_silence=released(ReleaseReason.LOCATION_SILENCE),
        released_end_of_day=released(ReleaseReason.END_OF_DAY),
        executions_total=sum(tally.executions.total for tally in tallies),
        executions_with_evidence=sum(tally.executions.with_evidence for tally in tallies),
        executions_flagged=sum(tally.executions.flagged for tally in tallies),
        alerts_total=sum(tally.alerts for tally in tallies) + unlinked_alerts,
    )


def _bucket(
    granularity: ReportGranularity, tallies: Sequence[_Tally], unlinked_alerts: dict[date, int]
) -> list[ComplianceRow]:
    tallies_by_period: dict[date, list[_Tally]] = defaultdict(list)
    for tally in tallies:
        tallies_by_period[_period_start(granularity, tally.day)].append(tally)
    alerts_by_period: dict[date, int] = defaultdict(int)
    for day, count in unlinked_alerts.items():
        alerts_by_period[_period_start(granularity, day)] += count
    return [
        ComplianceRow(
            period_label=_period_label(granularity, start),
            period_start=start,
            **asdict(_counts(tallies_by_period.get(start, []), alerts_by_period.get(start, 0))),
        )
        for start in sorted(tallies_by_period.keys() | alerts_by_period.keys())
    ]


def _tally(
    row: PointDay, filters: ReportFilters, executions: ExecutionCounts, alerts: int
) -> _Tally:
    worker = filters.field_worker_id
    return _Tally(
        day=row.day,
        outcome=row.outcome if worker is None else row.outcome_for(worker),
        release_reasons=row.release_reasons if worker is None else row.release_reasons_by(worker),
        executions=executions,
        alerts=alerts,
    )


async def _tallies(db: AsyncSession, filters: ReportFilters) -> list[_Tally]:
    """The filtered point-days with their executions and stop alerts; the outcome filter
    applies to the outcome as credited (to the worker, when narrowed to one)."""
    date_from, date_to, worker = filters.date_from, filters.date_to, filters.field_worker_id
    rows = await point_day_rows(
        db,
        date_from=date_from,
        date_to=date_to,
        company_id=filters.company_id,
        service_type_id=filters.service_type_id,
        building_id=filters.building_id,
        field_worker_id=worker,
    )
    executions = await executions_by_point_day(
        db, date_from=date_from, date_to=date_to, field_worker_id=worker
    )
    alerts = await stop_alerts_by_point_day(
        db, date_from=date_from, date_to=date_to, field_worker_id=worker
    )
    tallies = [
        _tally(
            row,
            filters,
            executions.get((row.service_point_id, row.day), _NO_EXECUTIONS),
            alerts.get((row.service_point_id, row.day), 0),
        )
        for row in rows
    ]
    return [t for t in tallies if filters.outcome is None or t.outcome == filters.outcome]


async def _resolve_filters_applied(db: AsyncSession, filters: ReportFilters) -> FilterApplied:
    """One point lookup per present UUID filter → its display name (spec §2 Ruling 8)."""
    worker = await db.get(FieldWorker, filters.field_worker_id) if filters.field_worker_id else None
    company = await db.get(ContractorCompany, filters.company_id) if filters.company_id else None
    service_type = (
        await db.get(ServiceType, filters.service_type_id) if filters.service_type_id else None
    )
    building = await db.get(Building, filters.building_id) if filters.building_id else None
    return FilterApplied(
        field_worker_name=worker.full_name if worker is not None else None,
        company_name=company.name if company is not None else None,
        service_type_name=service_type.name if service_type is not None else None,
        building_name=building.name if building is not None else None,
        outcome=OUTCOME_LABELS[filters.outcome] if filters.outcome is not None else None,
    )


async def build_compliance_report(
    db: AsyncSession, *, granularity: ReportGranularity, filters: ReportFilters
) -> ComplianceReport:
    """Count the filtered point-days and their activity into `granularity` buckets plus a
    totals row. Assumes `filters.date_from <= filters.date_to`."""
    tallies = await _tallies(db, filters)
    unlinked_alerts = await unlinked_alerts_by_day(db, filters)
    return ComplianceReport(
        granularity=granularity,
        date_from=filters.date_from,
        date_to=filters.date_to,
        filters_applied=await _resolve_filters_applied(db, filters),
        periods=_bucket(granularity, tallies, unlinked_alerts),
        totals=_counts(tallies, sum(unlinked_alerts.values())),
        generated_at=datetime.now(UTC),
    )
