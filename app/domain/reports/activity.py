"""The execution and alert indicators of the compliance report (RF45), linked to the point-days
of `point_day_rows` so the report's filters narrow them too.

- An **execution** belongs to the point-day of its stop (point, `service_date`).
- An **alert on a stop** (NO_EVIDENCE, POINT_IMPEDED of one stop, APPEAL_FILED) belongs to the
  point-day of that stop.
  The caller counts both only for the point-days its filters kept. Narrowed to a worker, only
  that worker's executions and the alerts on their own stops count.
- An **alert on a route without a stop** (BATCH_RELEASED, a rest-of-route POINT_IMPEDED) has no
  point. It counts on the route's day, can be narrowed by the route's worker and that worker's
  company, and is left out of a report filtered by type, building or outcome — it cannot be
  linked to those.
- A **route-less alert** (POINTS_UNSERVED) counts on the campus day it was raised, only in a
  report with no filter but the dates.

Read-only.

    executions = await executions_by_point_day(db, date_from=d1, date_to=d2, field_worker_id=None)
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker
from app.domain.execution.models import EvidenceItem, Execution, ExecutionReviewStatus
from app.domain.monitoring.models import Alert
from app.domain.reports.filters import ReportFilters
from app.domain.reports.point_days import campus_day_of
from app.domain.routing.models import Route, RouteStop

# (service point, service day) — the key of a point-day.
PointDayKey = tuple[uuid.UUID, date]


@dataclass(frozen=True)
class ExecutionCounts:
    total: int
    with_evidence: int
    # Waiting for a manager's review.
    flagged: int


async def executions_by_point_day(
    db: AsyncSession, *, date_from: date, date_to: date, field_worker_id: uuid.UUID | None
) -> dict[PointDayKey, ExecutionCounts]:
    """The executions of every point-day of the range that had any, optionally only one
    worker's."""
    has_evidence = select(EvidenceItem.id).where(EvidenceItem.execution_id == Execution.id).exists()
    flagged = Execution.review_status == ExecutionReviewStatus.PENDING_REVIEW
    stmt = (
        select(
            RouteStop.service_point_id,
            RouteStop.service_date,
            func.count(Execution.id),
            func.count().filter(has_evidence),
            func.count().filter(flagged),
        )
        .select_from(Execution)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .where(RouteStop.service_date.between(date_from, date_to))
        .group_by(RouteStop.service_point_id, RouteStop.service_date)
    )
    if field_worker_id is not None:
        stmt = stmt.where(Execution.field_worker_id == field_worker_id)
    return {
        (point_id, day): ExecutionCounts(total, with_evidence, flagged_count)
        for point_id, day, total, with_evidence, flagged_count in (await db.execute(stmt)).tuples()
    }


async def stop_alerts_by_point_day(
    db: AsyncSession, *, date_from: date, date_to: date, field_worker_id: uuid.UUID | None
) -> dict[PointDayKey, int]:
    """The alerts raised on a stop, per point-day of the range; optionally only the ones on
    one worker's stops."""
    stmt = (
        select(RouteStop.service_point_id, RouteStop.service_date, func.count(Alert.id))
        .select_from(Alert)
        .join(RouteStop, Alert.route_stop_id == RouteStop.id)
        .join(Route, RouteStop.route_id == Route.id)
        .where(RouteStop.service_date.between(date_from, date_to))
        .group_by(RouteStop.service_point_id, RouteStop.service_date)
    )
    if field_worker_id is not None:
        stmt = stmt.where(Route.field_worker_id == field_worker_id)
    rows = (await db.execute(stmt)).tuples()
    return {(point_id, day): count for point_id, day, count in rows}


async def unlinked_alerts_by_day(db: AsyncSession, filters: ReportFilters) -> dict[date, int]:
    """The alerts without a stop, per campus day, that the filters can be checked against (see
    the module docstring)."""
    if filters.service_type_id or filters.building_id or filters.outcome:
        return {}
    counts = dict((await db.execute(_route_alerts(filters))).tuples().all())
    if filters.field_worker_id or filters.company_id:
        return counts
    for day, count in (await db.execute(_route_less_alerts(filters))).tuples():
        counts[day] = counts.get(day, 0) + count
    return counts


def _route_alerts(filters: ReportFilters) -> Select[tuple[date, int]]:
    stmt = (
        select(Route.route_date, func.count(Alert.id))
        .select_from(Alert)
        .join(Route, Alert.route_id == Route.id)
        .join(FieldWorker, Route.field_worker_id == FieldWorker.id)
        .where(
            Alert.route_stop_id.is_(None),
            Route.route_date.between(filters.date_from, filters.date_to),
        )
        .group_by(Route.route_date)
    )
    if filters.field_worker_id is not None:
        stmt = stmt.where(Route.field_worker_id == filters.field_worker_id)
    if filters.company_id is not None:
        stmt = stmt.where(FieldWorker.contractor_company_id == filters.company_id)
    return stmt


def _route_less_alerts(filters: ReportFilters) -> Select[tuple[date, int]]:
    day = campus_day_of(Alert.created_at)
    return (
        select(day, func.count(Alert.id))
        .where(
            Alert.route_id.is_(None),
            Alert.route_stop_id.is_(None),
            day.between(filters.date_from, filters.date_to),
        )
        .group_by(day)
    )
