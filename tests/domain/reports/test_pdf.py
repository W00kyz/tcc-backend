from dataclasses import fields
from datetime import UTC, date, datetime

from app.domain.reports.compliance import (
    ComplianceCounts,
    ComplianceReport,
    ComplianceRow,
    FilterApplied,
    ReportGranularity,
)
from app.domain.reports.pdf import (
    _METRIC_HEADERS,
    _PRINTABLE_WIDTH_MM,
    _TABLE_WIDTH_MM,
    render_compliance_pdf,
)

_METRIC_FIELDS = tuple(field.name for field in fields(ComplianceCounts))


def _row(label: str, start: date, base: int) -> ComplianceRow:
    metrics = {field: base + offset for offset, field in enumerate(_METRIC_FIELDS)}
    return ComplianceRow(period_label=label, period_start=start, **metrics)


def _totals(count: int) -> ComplianceCounts:
    return ComplianceCounts(**dict.fromkeys(_METRIC_FIELDS, count))


def _report(rows: list[ComplianceRow], *, worker: str | None = "Ana Lima") -> ComplianceReport:
    return ComplianceReport(
        granularity=ReportGranularity.WEEKLY,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        filters_applied=FilterApplied(
            field_worker_name=worker,
            company_name="Limpa Tudo",
            service_type_name="Limpeza",
            building_name=None,
            outcome=None,
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


def test_table_fits_within_printable_width() -> None:
    assert _TABLE_WIDTH_MM <= _PRINTABLE_WIDTH_MM


def test_render_compliance_pdf_sanitizes_non_latin1_filter_name() -> None:
    rows = [_row("2026-W31", date(2026, 8, 3), 1)]

    result = render_compliance_pdf(_report(rows, worker="Nguyễn Vũ"))

    assert result[:4] == b"%PDF"


def test_one_header_per_indicator() -> None:
    assert len(_METRIC_HEADERS) == len(fields(ComplianceCounts)) == len(_METRIC_FIELDS)
