"""RF46 — render a `ComplianceReport` as a printable PDF (spec §2 Ruling 7).

Pure function: `ComplianceReport -> bytes`. No DB, no I/O beyond fpdf2's in-memory buffer.

Landscape A4 because the table is 12 columns (a "Período" column + the 10 point-day indicators
+ a "% concl." column) and will not fit portrait at a readable size. The built-in "Helvetica"
core font is Latin-1 only (fpdf2's `core_fonts_encoding`); pt-BR accented letters (á, ç, ã, õ)
are in Latin-1 so filter names render fine, but the em-dash and other non-Latin-1 glyphs are
not — every literal here stays ASCII-punctuation only.
"""

from dataclasses import fields

from fpdf import FPDF

from app.domain.reports.compliance import ComplianceCounts, ComplianceReport, ReportGranularity

_GRANULARITY_LABELS: dict[ReportGranularity, str] = {
    ReportGranularity.DAILY: "Diário",
    ReportGranularity.WEEKLY: "Semanal",
    ReportGranularity.MONTHLY: "Mensal",
}

# Abbreviated headers — 12 columns on landscape A4 is tight, so the metric headers are short.
# Order matches `_METRIC_FIELDS` (the fields of `ComplianceCounts`) plus the leading period and
# trailing rate.
_METRIC_HEADERS: tuple[str, ...] = (
    "Pontos",
    "Feitos",
    "P/ recurso",
    "Não atend.",
    "Liberados",
    "Impedidos",
    "Lib. resto",
    "Lib. campus",
    "Lib. sinal",
    "Lib. fim dia",
)
_METRIC_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(ComplianceCounts))

_PRINTABLE_WIDTH_MM = 277.0  # A4 landscape (297mm) minus 10mm margins each side
_PERIOD_COL_MM = 30.0
# Every row draws len(_METRIC_HEADERS) metric cells plus the trailing "% concl." cell, so the
# even split must be over that count + 1 — dividing by len(_METRIC_HEADERS) alone overflows the
# sheet and clips the "% concl." column off the right margin.
_METRIC_COL_MM = (_PRINTABLE_WIDTH_MM - _PERIOD_COL_MM) / (len(_METRIC_HEADERS) + 1)
_TABLE_WIDTH_MM = _PERIOD_COL_MM + _METRIC_COL_MM * (len(_METRIC_HEADERS) + 1)
_ROW_HEIGHT_MM = 5.0
_PAGE_BREAK_Y_MM = 180.0


def _latin1(text: str) -> str:
    """Degrade any glyph outside Latin-1 to `?` so a free-form DB name (a building or worker
    with e.g. `ễ`, `ł`, a curly apostrophe) can't crash the core-Helvetica PDF export."""
    return text.encode("latin-1", "replace").decode("latin-1")


def _format_rate(rate: float) -> str:
    return f"{rate * 100:.1f}%".replace(".", ",")


def _metadata_lines(report: ComplianceReport) -> list[str]:
    lines = [
        f"Período: {report.date_from:%d/%m/%Y} a {report.date_to:%d/%m/%Y}",
        f"Granularidade: {_GRANULARITY_LABELS[report.granularity]}",
    ]
    applied = report.filters_applied
    if applied.field_worker_name is not None:
        lines.append(f"Profissional: {_latin1(applied.field_worker_name)}")
    if applied.company_name is not None:
        lines.append(f"Empresa: {_latin1(applied.company_name)}")
    if applied.service_type_name is not None:
        lines.append(f"Tipo de serviço: {_latin1(applied.service_type_name)}")
    if applied.building_name is not None:
        lines.append(f"Prédio: {_latin1(applied.building_name)}")
    lines.append(f"Gerado em: {report.generated_at:%d/%m/%Y %H:%M}")
    return lines


def _draw_header_row(pdf: FPDF) -> None:
    pdf.set_font("Helvetica", style="B", size=6)
    pdf.cell(_PERIOD_COL_MM, _ROW_HEIGHT_MM, "Período", border=1)
    for header in (*_METRIC_HEADERS, "% concl."):
        pdf.cell(_METRIC_COL_MM, _ROW_HEIGHT_MM, header, border=1, align="C")
    pdf.ln(_ROW_HEIGHT_MM)


def _draw_data_row(pdf: FPDF, label: str, values: list[str], *, bold: bool) -> None:
    pdf.set_font("Helvetica", style="B" if bold else "", size=6)
    pdf.cell(_PERIOD_COL_MM, _ROW_HEIGHT_MM, label, border=1)
    for value in values:
        pdf.cell(_METRIC_COL_MM, _ROW_HEIGHT_MM, value, border=1, align="R")
    pdf.ln(_ROW_HEIGHT_MM)


def render_compliance_pdf(report: ComplianceReport) -> bytes:
    """Render `report` as a landscape-A4 PDF: a title, a metadata block, then a paginated
    table with one row per period and a bold TOTAIS row.

    Example:
        pdf_bytes = render_compliance_pdf(report)
        response = Response(pdf_bytes, media_type="application/pdf")
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.add_page()

    pdf.set_font("Helvetica", style="B", size=14)
    pdf.cell(0, 8, "Relatório de Conformidade dos Pontos de Serviço", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", size=8)
    for line in _metadata_lines(report):
        pdf.multi_cell(0, 4, text=line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    _draw_header_row(pdf)
    for row in report.periods:
        if pdf.get_y() > _PAGE_BREAK_Y_MM:
            pdf.add_page()
            _draw_header_row(pdf)
        values = [str(getattr(row, field)) for field in _METRIC_FIELDS]
        values.append(_format_rate(row.completion_rate))
        _draw_data_row(pdf, row.period_label, values, bold=False)

    if pdf.get_y() > _PAGE_BREAK_Y_MM:
        pdf.add_page()
        _draw_header_row(pdf)
    totals = report.totals
    total_values = [str(getattr(totals, field)) for field in _METRIC_FIELDS]
    total_values.append(_format_rate(totals.completion_rate))
    _draw_data_row(pdf, "TOTAIS", total_values, bold=True)

    return bytes(pdf.output())
