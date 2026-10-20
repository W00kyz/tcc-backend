import io
import zipfile
from datetime import UTC, date, datetime

from app.domain.reports.compliance import (
    ComplianceCounts,
    ComplianceReport,
    ComplianceRow,
    FilterApplied,
    ReportGranularity,
)
from app.domain.reports.xlsx import render_compliance_xlsx

_METRIC_FIELDS = (
    "points_total",
    "points_done",
    "points_done_by_appeal",
    "points_unserved",
    "points_released",
    "points_impeded",
    "released_route_remainder",
    "released_left_campus",
    "released_location_silence",
    "released_end_of_day",
)


def _row(label: str, start: date, base: int) -> ComplianceRow:
    metrics = {field: base + offset for offset, field in enumerate(_METRIC_FIELDS)}
    return ComplianceRow(period_label=label, period_start=start, **metrics)


def _report() -> ComplianceReport:
    rows = [
        _row("2026-08", date(2026, 8, 1), 2),
        _row("2026-09", date(2026, 9, 1), 6),
    ]
    totals = ComplianceCounts(**dict.fromkeys(_METRIC_FIELDS, 8))
    return ComplianceReport(
        granularity=ReportGranularity.MONTHLY,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 30),
        filters_applied=FilterApplied(
            field_worker_name="João Conceição",
            company_name=None,
            service_type_name=None,
            building_name="Bloco CN",
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


def test_xlsx_headers_name_the_point_by_day_indicators() -> None:
    with zipfile.ZipFile(io.BytesIO(render_compliance_xlsx(_report()))) as archive:
        strings = archive.read("xl/sharedStrings.xml").decode()

    for header in ("Pontos não atendidos", "Feitos por recurso", "Impedidos", "fim do expediente"):
        assert header in strings, header
    assert "reatribu" not in strings
    assert "Status da rota" not in strings
    assert "Empresa: " not in strings  # no company filter in this report
