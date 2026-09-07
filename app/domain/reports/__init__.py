"""RF43-RF46 — consolidated reporting read model (Etapa 10).

`filters.ReportFilters` is the shared filter axis (date range, worker, service type, building,
route status) applied uniformly to reports; `compliance.build_compliance_report` aggregates
routes/stops/executions/impediments/alerts into daily/weekly/monthly buckets. Read-only: no
`flush`, no `commit` (spec §2 Ruling 2)."""
