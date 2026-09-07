import io
import zipfile
from datetime import UTC, date, datetime

from app.domain.reports.compliance import (
    ComplianceReport,
    ComplianceRow,
    ComplianceTotals,
    FilterApplied,
    ReportGranularity,
)
from app.domain.reports.xlsx import render_compliance_xlsx

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
    return ComplianceRow(period_label=label, period_start=start, completion_rate=0.5, **metrics)


def _report() -> ComplianceReport:
    rows = [
        _row("2026-08", date(2026, 8, 1), 2),
        _row("2026-09", date(2026, 9, 1), 6),
    ]
    totals = ComplianceTotals(completion_rate=0.75, **dict.fromkeys(_METRIC_FIELDS, 8))
    return ComplianceReport(
        granularity=ReportGranularity.MONTHLY,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        filters_applied=FilterApplied(
            field_worker_name="João Conceição",
            service_type_name=None,
            building_name="Bloco CN",
            route_status="Concluída",
        ),
        periods=rows,
        totals=totals,
        generated_at=datetime(2026, 9, 7, 14, 30, tzinfo=UTC),
    )


def test_render_compliance_xlsx_returns_xlsx_zip() -> None:
    result = render_compliance_xlsx(_report())

    assert result[:2] == b"PK"
    assert len(result) > 500

    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
