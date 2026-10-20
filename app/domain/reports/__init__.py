"""RF43-RF46 — consolidated reporting read model (Etapa 10; spec 2026-10-20-alocacao-automatica
§8.7).

`point_days.point_day_rows` is the base of every count — each typed point on each service day it
existed, counted once as done, done by appeal or unserved; `filters.ReportFilters` is the shared
filter axis (date range, worker, company, service type, building) applied uniformly to reports;
`compliance.build_compliance_report` counts the point-days into daily/weekly/monthly buckets.
Read-only: no `commit` (spec §2 Ruling 2)."""
