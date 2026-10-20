"""The shared report filter axis (spec §2 Ruling 5, RF44).

`ReportFilters` is passed to `build_compliance_report` and on to `point_day_rows`. The same axes
back the panel's report page: date range (mandatory), field worker, company, service type and
building. The route-status filter went with counting by route (spec 2026-10-20-alocacao-
automatica §8.7): a point-day is not a route. The endpoint validates `date_from <= date_to` and
caps the span (spec §2 Ruling 5); the domain builder assumes a valid range.

Example:
    filters = ReportFilters(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
"""

import uuid
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ReportFilters:
    date_from: date
    date_to: date
    field_worker_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    service_type_id: uuid.UUID | None = None
    building_id: uuid.UUID | None = None
