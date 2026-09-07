from datetime import UTC, date, datetime

from app.domain.reports.compliance import (
    ComplianceReport,
    ComplianceRow,
    ComplianceTotals,
    FilterApplied,
    ReportGranularity,
)
from app.domain.reports.pdf import render_compliance_pdf

_METRIC_FIELDS = (
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


def _row(label: str, start: date, base: int) -> ComplianceRow:
    metrics = {field: base + offset for offset, field in enumerate(_METRIC_FIELDS)}
    return ComplianceRow(
        period_label=label,
        period_start=start,
        completion_rate=0.873,
        **metrics,
    )


def _totals(count: int) -> ComplianceTotals:
    metrics = dict.fromkeys(_METRIC_FIELDS, count)
    return ComplianceTotals(completion_rate=0.912, **metrics)


def _report(rows: list[ComplianceRow], *, worker: str | None = "Ana Lima") -> ComplianceReport:
    return ComplianceReport(
        granularity=ReportGranularity.WEEKLY,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        filters_applied=FilterApplied(
            field_worker_name=worker,
            service_type_name="Limpeza",
            building_name=None,
            route_status=None,
        ),
        periods=rows,
        totals=_totals(len(rows)),
        generated_at=datetime(2026, 9, 7, 14, 30, tzinfo=UTC),
    )


def test_render_compliance_pdf_returns_pdf_bytes() -> None:
    rows = [
        _row("2026-W31", date(2026, 8, 3), 1),
        _row("2026-W32", date(2026, 8, 10), 5),
        _row("2026-W33", date(2026, 8, 17), 9),
    ]
    result = render_compliance_pdf(_report(rows))

    assert result[:4] == b"%PDF"
    assert len(result) > 1000


def test_render_compliance_pdf_paginates_large_report() -> None:
    rows = [_row(f"2026-{i:04d}", date(2026, 1, 1), i) for i in range(400)]

    result = render_compliance_pdf(_report(rows))

    assert result[:4] == b"%PDF"


def test_render_compliance_pdf_accepts_ptbr_accented_filter_name() -> None:
    rows = [_row("2026-W31", date(2026, 8, 3), 1)]

    result = render_compliance_pdf(_report(rows, worker="João Conceição"))

    assert result[:4] == b"%PDF"
