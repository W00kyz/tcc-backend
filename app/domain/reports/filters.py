"""The shared report filter axis (spec §2 Ruling 5, RF44).

`ReportFilters` is passed to `build_compliance_report` and, later, to the export renderers. The
same five axes back the panel's report page: date range (mandatory), field worker, service type,
building and route status. The endpoint validates `date_from <= date_to` and caps the span
(spec §2 Ruling 5); the domain builder assumes a valid range.

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
    service_type_id: uuid.UUID | None = None
    building_id: uuid.UUID | None = None
    route_status: str | None = None  # a RouteStatus value
