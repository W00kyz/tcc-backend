"""Etapa 10 — the `/reports` surface for the manager dashboard (RF44/RF45/RF46, spec §2
Rulings 5, 6). One JSON endpoint plus two export endpoints, all three sharing
`build_compliance_report`; the exports pipe its result through the pure `render_compliance_pdf`
/ `render_compliance_xlsx` renderers.

The three endpoints take the same query axis. `_parse_report_params` is the single place that
turns the raw params into a validated `(ReportGranularity, ReportFilters)` pair — it enforces
`date_to >= date_from` and the 366-day span cap (spec §2 Ruling 5), both as `422`.
`granularity` is declared as the enum in each signature so FastAPI coerces it and answers `422`
on a bad value without our help.
"""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.identity.models import User, UserRole
from app.domain.reports.compliance import (
    ComplianceReport,
    ReportGranularity,
    build_compliance_report,
)
from app.domain.reports.filters import ReportFilters
from app.domain.reports.pdf import render_compliance_pdf
from app.domain.reports.xlsx import render_compliance_xlsx

reports_router = APIRouter(prefix="/reports", tags=["reports"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Span cap for a single report (spec §2 Ruling 5) — generous for "monthly over the pilot",
# tight enough to keep one request from scanning an absurd date range.
_MAX_SPAN_DAYS = 366


class FilterAppliedOut(BaseModel):
    """Mirror of `app.domain.reports.compliance.FilterApplied` — the human-readable echo of the
    filters that were applied, with UUIDs already resolved to names by the builder."""

    model_config = ConfigDict(from_attributes=True)

    field_worker_name: str | None
    company_name: str | None
    service_type_name: str | None
    building_name: str | None


class ComplianceTotalsOut(BaseModel):
    """Mirror of `ComplianceCounts` — the point-day indicators (spec 2026-10-20-alocacao-
    automatica §8.7) plus the derived `completion_rate`. `points_done` includes the ones done by
    appeal; `points_impeded` is the release counter of the IMPEDIMENT reason."""

    model_config = ConfigDict(from_attributes=True)

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
    completion_rate: float


class ComplianceRowOut(ComplianceTotalsOut):
    """Mirror of `ComplianceRow` — one granularity bucket: its identity plus the indicators."""

    period_label: str
    period_start: date


class ComplianceReportOut(BaseModel):
    """Mirror of `ComplianceReport`. The domain builder already returns frozen, JSON-serialisable
    dataclasses; this thin `...Out` layer (built via `model_validate`, `from_attributes=True`)
    keeps the API schema explicit and decoupled from the dataclass without restating the query
    or re-deriving anything."""

    model_config = ConfigDict(from_attributes=True)

    granularity: ReportGranularity
    date_from: date
    date_to: date
    filters_applied: FilterAppliedOut
    periods: list[ComplianceRowOut]
    totals: ComplianceTotalsOut
    generated_at: datetime


def _parse_report_params(
    granularity: ReportGranularity,
    date_from: date,
    date_to: date,
    field_worker_id: Annotated[UUID | None, Query()] = None,
    company_id: Annotated[UUID | None, Query()] = None,
    service_type_id: Annotated[UUID | None, Query()] = None,
    building_id: Annotated[UUID | None, Query()] = None,
) -> tuple[ReportGranularity, ReportFilters]:
    """Validate the shared query axis and build the `ReportFilters`. Raises `HTTPException(422)`
    on an inverted range or a span over `_MAX_SPAN_DAYS`."""
    if date_to < date_from:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"date_to {date_to} is before date_from {date_from}; expected date_to >= date_from.",
        )
    if (date_to - date_from).days > _MAX_SPAN_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"range {date_from}..{date_to} spans more than {_MAX_SPAN_DAYS} days; "
            f"expected a range of at most {_MAX_SPAN_DAYS} days.",
        )
    filters = ReportFilters(
        date_from=date_from,
        date_to=date_to,
        field_worker_id=field_worker_id,
        company_id=company_id,
        service_type_id=service_type_id,
        building_id=building_id,
    )
    return granularity, filters


# The query params are identical across the three endpoints; declaring them once here and
# spreading via `Depends` keeps the signatures honest and the OpenAPI schema consistent.
_ReportParams = Annotated[tuple[ReportGranularity, ReportFilters], Depends(_parse_report_params)]


@reports_router.get("/compliance", response_model=ComplianceReportOut)
async def get_compliance_report(
    params: _ReportParams, _actor: _Manager, db: _Db
) -> ComplianceReportOut:
    """RF45 — the compliance read model as JSON: the granularity buckets with the point-day
    indicators plus `completion_rate`, a totals row, and the resolved `filters_applied`."""
    granularity, filters = params
    report = await build_compliance_report(db, granularity=granularity, filters=filters)
    return ComplianceReportOut.model_validate(report)


async def _build(
    db: AsyncSession, params: tuple[ReportGranularity, ReportFilters]
) -> ComplianceReport:
    granularity, filters = params
    return await build_compliance_report(db, granularity=granularity, filters=filters)


def _attachment_headers(filters: ReportFilters, suffix: str) -> dict[str, str]:
    name = f"relatorio-conformidade-{filters.date_from}-{filters.date_to}.{suffix}"
    return {"Content-Disposition": f'attachment; filename="{name}"'}


@reports_router.get("/compliance.pdf")
async def get_compliance_report_pdf(params: _ReportParams, _actor: _Manager, db: _Db) -> Response:
    """RF46 — the same report rendered as a printable landscape-A4 PDF attachment."""
    report = await _build(db, params)
    return Response(
        content=render_compliance_pdf(report),
        media_type="application/pdf",
        headers=_attachment_headers(params[1], "pdf"),
    )


@reports_router.get("/compliance.xlsx")
async def get_compliance_report_xlsx(params: _ReportParams, _actor: _Manager, db: _Db) -> Response:
    """RF46 — the same report as an XLSX workbook attachment."""
    report = await _build(db, params)
    return Response(
        content=render_compliance_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_attachment_headers(params[1], "xlsx"),
    )
