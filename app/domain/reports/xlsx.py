"""RF46 — render a `ComplianceReport` as an XLSX workbook (spec §2 Ruling 7).

Pure function: `ComplianceReport -> bytes`. No DB, no I/O beyond the in-memory `BytesIO`.

One sheet "Conformidade": a merged metadata row, a blank row, a bold header row with the full
(non-abbreviated) pt-BR indicator names, one data row per period, and a bold TOTAIS row.
`completion_rate` is written as its raw float with a `0.0%` number format so the cell stays a
real number for the reader to sort or chart.
"""

import io

import xlsxwriter

from app.domain.reports.compliance import ComplianceReport, ReportGranularity

_GRANULARITY_LABELS: dict[ReportGranularity, str] = {
    ReportGranularity.DAILY: "Diário",
    ReportGranularity.WEEKLY: "Semanal",
    ReportGranularity.MONTHLY: "Mensal",
}

_HEADERS: tuple[str, ...] = (
    "Período",
    "Rotas",
    "Rotas iniciadas",
    "Rotas concluídas",
    "Rotas canceladas",
    "Pontos",
    "Pontos feitos",
    "Pontos pendentes",
    "Pontos reatribuídos",
    "Execuções",
    "Execuções com evidência",
    "Execuções sinalizadas",
    "Impedimentos",
    "Alertas",
    "% de conclusão",
)
_METRIC_FIELDS: tuple[str, ...] = (
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
_LAST_COL = len(_HEADERS) - 1  # 14


def _metadata_line(report: ComplianceReport) -> str:
    parts = [
        f"Período: {report.date_from:%d/%m/%Y} a {report.date_to:%d/%m/%Y}",
        f"Granularidade: {_GRANULARITY_LABELS[report.granularity]}",
    ]
    applied = report.filters_applied
    if applied.field_worker_name is not None:
        parts.append(f"Profissional: {applied.field_worker_name}")
    if applied.service_type_name is not None:
        parts.append(f"Tipo de serviço: {applied.service_type_name}")
    if applied.building_name is not None:
        parts.append(f"Prédio: {applied.building_name}")
    if applied.route_status is not None:
        parts.append(f"Status da rota: {applied.route_status}")
    parts.append(f"Gerado em: {report.generated_at:%d/%m/%Y %H:%M}")
    return " | ".join(parts)


def render_compliance_xlsx(report: ComplianceReport) -> bytes:
    """Render `report` as an XLSX workbook and return its bytes.

    Example:
        xlsx_bytes = render_compliance_xlsx(report)
        response = Response(xlsx_bytes, media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ))
    """
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet("Conformidade")

    meta_fmt = workbook.add_format({"italic": True})
    header_fmt = workbook.add_format({"bold": True, "bg_color": "#D9E2F3", "border": 1})
    total_fmt = workbook.add_format({"bold": True})
    pct_fmt = workbook.add_format({"num_format": "0.0%"})
    total_pct_fmt = workbook.add_format({"bold": True, "num_format": "0.0%"})

    sheet.merge_range(0, 0, 0, _LAST_COL, _metadata_line(report), meta_fmt)

    for col, header in enumerate(_HEADERS):
        sheet.write(2, col, header, header_fmt)

    row_index = 3
    for period in report.periods:
        sheet.write(row_index, 0, period.period_label)
        for offset, field in enumerate(_METRIC_FIELDS, start=1):
            sheet.write(row_index, offset, getattr(period, field))
        sheet.write(row_index, _LAST_COL, period.completion_rate, pct_fmt)
        row_index += 1

    totals = report.totals
    sheet.write(row_index, 0, "TOTAIS", total_fmt)
    for offset, field in enumerate(_METRIC_FIELDS, start=1):
        sheet.write(row_index, offset, getattr(totals, field), total_fmt)
    sheet.write(row_index, _LAST_COL, totals.completion_rate, total_pct_fmt)

    sheet.set_column(0, 0, 14)
    sheet.set_column(1, _LAST_COL, 12)
    sheet.freeze_panes(3, 1)

    workbook.close()
    return buffer.getvalue()
