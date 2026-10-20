"""RF45 — the service-compliance read model (spec §2 Rulings 4, 5, 8; spec
2026-10-20-alocacao-automatica §8.7).

`build_compliance_report` counts the point-days of `point_day_rows` — every typed point on every
service day it existed, counted once — into daily / weekly / monthly buckets: how many were done
(by a stop or by an accepted appeal), how many went unserved, and how many were released, by
reason. Only buckets with at least one point-day are returned (spec §2 Ruling 4), so weekends
never show.

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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, ServiceType
from app.domain.reports.filters import ReportFilters
from app.domain.reports.point_days import PointDay, point_day_rows
from app.domain.routing.models import ReleaseReason


class ReportGranularity(enum.StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


# The release counters per reason. IMPEDIMENT is not here: an impeded point has its own
# indicator, `points_impeded`.
_RELEASE_REASON_FIELDS: dict[ReleaseReason, str] = {
    ReleaseReason.ROUTE_REMAINDER: "released_route_remainder",
    ReleaseReason.LEFT_CAMPUS: "released_left_campus",
    ReleaseReason.LOCATION_SILENCE: "released_location_silence",
    ReleaseReason.END_OF_DAY: "released_end_of_day",
}


@dataclass(frozen=True)
class ComplianceCounts:
    """The indicators of a bucket, all in point-days. `points_done` includes the ones done by
    appeal. A point-day released twice for the same reason counts once in that reason's
    counter; released for two reasons, it counts in both."""

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


def _counts(rows: Sequence[PointDay]) -> dict[str, int]:
    done = sum(1 for row in rows if row.outcome != "UNSERVED")
    counts = {
        "points_total": len(rows),
        "points_done": done,
        "points_done_by_appeal": sum(1 for row in rows if row.outcome == "DONE_BY_APPEAL"),
        "points_unserved": len(rows) - done,
        "points_released": sum(1 for row in rows if row.released_count),
        "points_impeded": sum(1 for row in rows if row.impeded_count),
    }
    for reason, name in _RELEASE_REASON_FIELDS.items():
        counts[name] = sum(1 for row in rows if reason in row.release_reasons)
    return counts


def _bucket(granularity: ReportGranularity, rows: Sequence[PointDay]) -> list[ComplianceRow]:
    by_period: dict[date, list[PointDay]] = defaultdict(list)
    for row in rows:
        by_period[_period_start(granularity, row.day)].append(row)
    return [
        ComplianceRow(
            period_label=_period_label(granularity, start),
            period_start=start,
            **_counts(by_period[start]),
        )
        for start in sorted(by_period)
    ]


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
    )


async def build_compliance_report(
    db: AsyncSession, *, granularity: ReportGranularity, filters: ReportFilters
) -> ComplianceReport:
    """Count the filtered point-days into `granularity` buckets plus a totals row. Assumes
    `filters.date_from <= filters.date_to`."""
    rows = await point_day_rows(
        db,
        date_from=filters.date_from,
        date_to=filters.date_to,
        company_id=filters.company_id,
        service_type_id=filters.service_type_id,
        building_id=filters.building_id,
        field_worker_id=filters.field_worker_id,
    )
    return ComplianceReport(
        granularity=granularity,
        date_from=filters.date_from,
        date_to=filters.date_to,
        filters_applied=await _resolve_filters_applied(db, filters),
        periods=_bucket(granularity, rows),
        totals=ComplianceCounts(**_counts(rows)),
        generated_at=datetime.now(UTC),
    )
