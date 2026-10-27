"""The shared report filter axis (spec §2 Ruling 5, RF44).

`ReportFilters` is passed to `build_compliance_report` and on to `point_day_rows`. The same axes
back the panel's report page: date range (mandatory), field worker, company, service type,
building and status. Counting by point-day (spec 2026-10-20-alocacao-automatica §8.7), the
status axis is the point-day's outcome — done, done by appeal, unserved. The endpoint validates
`date_from <= date_to` and caps the span (spec §2 Ruling 5); the domain builder assumes a valid
range.

Example:
    filters = ReportFilters(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
"""

import uuid
from dataclasses import dataclass
from datetime import date

from app.domain.reports.point_days import PointDayOutcome


@dataclass(frozen=True)
class ReportFilters:
    date_from: date
    date_to: date
    field_worker_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    service_type_id: uuid.UUID | None = None
    building_id: uuid.UUID | None = None
    # Narrowed to a worker, the outcome credited to them (`PointDay.outcome_for`).
    outcome: PointDayOutcome | None = None
