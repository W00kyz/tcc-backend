"""The base of every service count (spec 2026-10-20-alocacao-automatica §8.7 "Base de cálculo"):
one typed point on one service day, counted once.

A point counts on day D when its service type belongs to a company and it existed on D (created
by the end of the campus day). It was **done** on D when it has a DONE stop that day, **done by
appeal** when instead an appeal on one of its stops of D was accepted, and **unserved**
otherwise. A point released and then done by somebody else counts once, as done — and its
release still shows in `release_reasons`.

`served_on` is the same rule as a SQL predicate, for the day-end alert and the board.

Read-only: no `flush` beyond recreating a missing settings row, no `commit`.

    rows = await point_day_rows(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import (
    ColumnElement,
    Date,
    String,
    Subquery,
    and_,
    cast,
    column,
    exists,
    func,
    or_,
    select,
    true,
    values,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campus_clock import CAMPUS_TIMEZONE
from app.domain.appeals.models import Appeal, AppealStatus
from app.domain.catalog.models import ContractorCompany, Floor, ServicePoint, ServiceType
from app.domain.routing.models import ReleaseReason, Route, RouteStop, RouteStopStatus
from app.domain.settings.campus import is_service_day
from app.domain.settings.service import get_settings_row

PointDayOutcome = Literal["DONE", "DONE_BY_APPEAL", "UNSERVED"]


@dataclass(frozen=True)
class PointDay:
    day: date
    service_point_id: uuid.UUID
    service_type_id: uuid.UUID
    company_id: uuid.UUID
    building_id: uuid.UUID
    outcome: PointDayOutcome
    # The worker of the DONE stop, else the one whose appeal was accepted; None when unserved.
    done_by: uuid.UUID | None
    # Why the point went back to the pool that day, one entry per release, oldest first.
    release_reasons: tuple[ReleaseReason, ...]

    @property
    def released_count(self) -> int:
        return len(self.release_reasons)

    @property
    def impeded_count(self) -> int:
        return self.release_reasons.count(ReleaseReason.IMPEDIMENT)


def _stop_facts(date_from: date, date_to: date) -> Subquery:
    """One row per (point, service day) that had any stop in the range: who did it with a DONE
    stop, whose appeals were accepted, why it was released, and who held a stop for it.

    One grouped pass over the range's stops, joined to the point-days afterwards — not three
    correlated subqueries per point-day, which a year-long report could not afford."""
    accepted = and_(Appeal.route_stop_id == RouteStop.id, Appeal.status == AppealStatus.ACCEPTED)
    return (
        select(
            RouteStop.service_point_id,
            RouteStop.service_date,
            # The exclusivity index admits one DONE stop per point and day.
            func.array_agg(Route.field_worker_id)
            .filter(RouteStop.status == RouteStopStatus.DONE)
            .label("done_by"),
            func.array_agg(aggregate_order_by(Appeal.field_worker_id, Appeal.reviewed_at))
            .filter(Appeal.id.is_not(None))
            .label("appealed_by"),
            # As text: plain strings back, turned into `ReleaseReason` in `_to_point_day`.
            func.array_agg(
                aggregate_order_by(cast(RouteStop.release_reason, String), RouteStop.released_at)
            )
            .filter(RouteStop.status == RouteStopStatus.RELEASED)
            .label("release_reasons"),
            func.array_agg(Route.field_worker_id).label("holders"),
        )
        .join(Route, RouteStop.route_id == Route.id)
        .outerjoin(Appeal, accepted)
        .where(RouteStop.service_date.between(date_from, date_to))
        .group_by(RouteStop.service_point_id, RouteStop.service_date)
        .subquery("stop_facts")
    )


def served_on(day: date) -> ColumnElement[bool]:
    """Whether the point (correlated on `ServicePoint`) was served on `day`: a DONE stop that
    day or an accepted appeal on one of its stops of that day.

    >>> select(func.count(ServicePoint.id)).where(~served_on(today))
    """
    facts = _stop_facts(day, day)
    return exists().where(
        facts.c.service_point_id == ServicePoint.id,
        or_(facts.c.done_by.is_not(None), facts.c.appealed_by.is_not(None)),
    )


def _campus_creation_day() -> ColumnElement[date]:
    """The campus calendar day the point was created on — a point created at 22:00 on campus
    is already the next day in UTC."""
    return cast(func.timezone(CAMPUS_TIMEZONE.key, ServicePoint.created_at), Date)


async def _service_days(db: AsyncSession, date_from: date, date_to: date) -> list[date]:
    settings = await get_settings_row(db)
    span = (date_to - date_from).days + 1
    every_day = (date_from + timedelta(days=offset) for offset in range(span))
    return [day for day in every_day if is_service_day(settings, day)]


def _outcome(
    done_by: list[uuid.UUID] | None, appealed_by: list[uuid.UUID] | None
) -> tuple[PointDayOutcome, uuid.UUID | None]:
    if done_by:
        return "DONE", done_by[0]
    if appealed_by:
        return "DONE_BY_APPEAL", appealed_by[0]
    return "UNSERVED", None


async def point_day_rows(
    db: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    company_id: uuid.UUID | None = None,
    service_type_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    field_worker_id: uuid.UUID | None = None,
) -> list[PointDay]:
    """Every typed point on every service day of `date_from..date_to` it existed, ordered by
    day. The filters narrow by the point's company, type and building; `field_worker_id` keeps
    the point-days that worker held a stop for — each still with its one outcome, whoever
    served it.

        rows = await point_day_rows(db, date_from=monday, date_to=friday, company_id=company.id)
    """
    service_days = await _service_days(db, date_from, date_to)
    if not service_days:
        return []
    days = values(column("day", Date), name="service_days").data([(day,) for day in service_days])
    facts = _stop_facts(date_from, date_to)
    stmt = (
        select(
            days.c.day,
            ServicePoint.id,
            ServiceType.id,
            ContractorCompany.id,
            Floor.building_id,
            facts.c.done_by,
            facts.c.appealed_by,
            facts.c.release_reasons,
        )
        .select_from(days)
        .join(ServicePoint, true())
        .join(ServiceType, ServicePoint.service_type_id == ServiceType.id)
        .join(ContractorCompany, ServiceType.contractor_company_id == ContractorCompany.id)
        .join(Floor, ServicePoint.floor_id == Floor.id)
        .outerjoin(
            facts,
            and_(facts.c.service_point_id == ServicePoint.id, facts.c.service_date == days.c.day),
        )
        .where(_campus_creation_day() <= days.c.day)
        .order_by(days.c.day, ServicePoint.id)
    )
    if company_id is not None:
        stmt = stmt.where(ContractorCompany.id == company_id)
    if service_type_id is not None:
        stmt = stmt.where(ServiceType.id == service_type_id)
    if building_id is not None:
        stmt = stmt.where(Floor.building_id == building_id)
    if field_worker_id is not None:
        stmt = stmt.where(facts.c.holders.any(field_worker_id))
    return [_to_point_day(row) for row in (await db.execute(stmt)).tuples()]


def _to_point_day(
    row: tuple[
        date,
        uuid.UUID,
        uuid.UUID,
        uuid.UUID,
        uuid.UUID,
        list[uuid.UUID] | None,
        list[uuid.UUID] | None,
        list[str] | None,
    ],
) -> PointDay:
    day, point_id, type_id, company_id, building_id, done_by, appealed_by, reasons = row
    outcome, who = _outcome(done_by, appealed_by)
    return PointDay(
        day=day,
        service_point_id=point_id,
        service_type_id=type_id,
        company_id=company_id,
        building_id=building_id,
        outcome=outcome,
        done_by=who,
        release_reasons=tuple(ReleaseReason(reason) for reason in reasons or ()),
    )
