"""Cenário de relatórios ponta a ponta — relatório mensal de conformidade + exportação
(requisitos §7 RF43-RF46, design §2 Ruling 9 e §7).

O deliverable da Etapa 10: um mês real de rotas acumuladas (uma por dia útil de setembro/2026)
com stops, execuções (a maioria com evidência, uma com conclusão manual, uma sinalizada para
revisão), um impedimento e um alerta é consolidado por `GET /reports/compliance` nas três
granularidades e exportado em PDF e XLSX. Cada hop roda contra o Postgres/PostGIS real via uma
requisição HTTP de verdade (`TestClient`), autenticada como gestora.

O que fica pinado aqui:
- os 13 indicadores + `completion_rate` de um bucket MENSAL, conferidos contra valores
  recomputados no teste a partir das constantes do seed (`_PLAN`), nunca lidos de volta da
  resposta;
- a linha de TOTAIS igual ao único bucket mensal;
- os buckets DIÁRIO (um por dia útil, sem fim de semana, crescentes) e SEMANAL (as semanas ISO
  que setembro/2026 abrange — 2026-W36..W40);
- o PDF (`%PDF`, > 1 KB, `application/pdf`, `Content-Disposition` de anexo) e o XLSX (`PK`,
  contém `xl/worksheets/sheet1.xml`, content-type de planilha);
- o filtro `field_worker_id` estreitando as métricas para as rotas de um profissional.

O que fica como passo manual do autor (mesmo tratamento de `test_scenario_5_alerts.py`):
- baixar os dois arquivos pelo painel com dados reais do piloto e conferir o layout num leitor
  de PDF / editor de planilha de verdade.

Estratégia de seed: **inserção direta** via os modelos de domínio. Montar um mês de rotas pelos
serviços reais (`create_route` + check-in/check-out por HTTP) seria dezenas de requisições e um
fixture gigante para zero cobertura extra do read model — que é o alvo. As rotas, stops,
execuções, evidências, conclusão manual, impedimento e alerta são inseridos direto; o único
caminho por serviço real exercitado aqui é o endpoint `/reports/compliance` sob teste.

Marcado `slow`, fora do run padrão (`-m "not slow"` no `addopts`); selecionar com
`uv run pytest -m slow`.
"""

import io
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    ManualCompletion,
)
from app.domain.identity.models import User, UserRole
from app.domain.impediments.models import Impediment, ImpedimentReason, ImpedimentScope
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    StopAssignment,
)
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PASSWORD = "senha-forte-o-suficiente"
_MANAGER_EMAIL = "gestora@pu.ufcg.edu.br"
_WORKER_EMAIL = "operario@empresa.com"
_MONTH_FROM = date(2026, 9, 1)
_MONTH_TO = date(2026, 9, 30)

# --- Seed constants — the whole scenario is derived from these so the assertions can recompute
# every expected metric instead of trusting the response. -----------------------------------
_WORKER_B_ROUTE_INDEXES = frozenset({3, 11, 19})  # rotas do segundo profissional
_CANCELLED_ROUTE_INDEXES = frozenset({7, 14})
_IN_PROGRESS_ROUTE_INDEX = 21
_MANUAL_COMPLETION_STOP = (1, 0)  # (índice da rota, índice do stop) — conclusão manual, sem foto
_FLAGGED_STOP = (2, 0)  # execução com review_status PENDING_REVIEW
_REASSIGNED_STOP = (0, 0)  # stop com >= 2 linhas em stop_assignments
_IMPEDIMENT_ROUTE_INDEX = 4
_ALERT_ROUTE_INDEX = 6


@dataclass(frozen=True)
class _ExecSpec:
    has_evidence: bool
    has_manual_completion: bool
    flagged: bool


@dataclass
class _StopSpec:
    status: RouteStopStatus
    execution: _ExecSpec | None
    reassigned: bool = False


@dataclass
class _RouteSpec:
    index: int
    route_date: date
    worker: str  # "a" | "b"
    status: RouteStatus
    stops: list[_StopSpec] = field(default_factory=list)
    has_impediment: bool = False
    has_alert: bool = False


def _weekdays(month_from: date, month_to: date) -> list[date]:
    days = (month_to - month_from).days + 1
    return [
        d
        for d in (month_from + timedelta(days=i) for i in range(days))
        if d.weekday() < 5  # segunda a sexta
    ]


def _build_plan() -> list[_RouteSpec]:
    """One route per weekday of September 2026, varied so no metric is trivially uniform."""
    plan: list[_RouteSpec] = []
    for i, route_date in enumerate(_weekdays(_MONTH_FROM, _MONTH_TO)):
        if i in _CANCELLED_ROUTE_INDEXES:
            status = RouteStatus.CANCELLED
        elif i == _IN_PROGRESS_ROUTE_INDEX:
            status = RouteStatus.IN_PROGRESS
        else:
            status = RouteStatus.DONE
        worker = "b" if i in _WORKER_B_ROUTE_INDEXES else "a"
        n_stops = 2 + (i % 2)  # 2 ou 3 stops
        stops: list[_StopSpec] = []
        for s in range(n_stops):
            is_last = s == n_stops - 1
            pending = is_last and i % 3 == 0
            stop_status = RouteStopStatus.PENDING if pending else RouteStopStatus.DONE
            has_execution = stop_status is RouteStopStatus.DONE and status in (
                RouteStatus.DONE,
                RouteStatus.IN_PROGRESS,
            )
            spec = _ExecSpec(has_evidence=True, has_manual_completion=False, flagged=False)
            stops.append(_StopSpec(stop_status, spec if has_execution else None))
        plan.append(
            _RouteSpec(
                index=i,
                route_date=route_date,
                worker=worker,
                status=status,
                stops=stops,
                has_impediment=i == _IMPEDIMENT_ROUTE_INDEX,
                has_alert=i == _ALERT_ROUTE_INDEX,
            )
        )

    ri, si = _MANUAL_COMPLETION_STOP
    plan[ri].stops[si].execution = _ExecSpec(
        has_evidence=False, has_manual_completion=True, flagged=False
    )
    ri, si = _FLAGGED_STOP
    plan[ri].stops[si].execution = _ExecSpec(
        has_evidence=True, has_manual_completion=False, flagged=True
    )
    ri, si = _REASSIGNED_STOP
    plan[ri].stops[si].reassigned = True
    return plan


_PLAN = _build_plan()


# --- Expected metrics, recomputed straight from `_PLAN` -------------------------------------
def _executions(plan: list[_RouteSpec]) -> list[_ExecSpec]:
    return [st.execution for r in plan for st in r.stops if st.execution is not None]


def _expected_metrics(plan: list[_RouteSpec]) -> dict[str, int | float]:
    all_stops = [st for r in plan for st in r.stops]
    execs = _executions(plan)
    stops_total = len(all_stops)
    stops_done = sum(1 for st in all_stops if st.status is RouteStopStatus.DONE)
    return {
        "routes_total": len(plan),
        "routes_started": sum(
            1 for r in plan if r.status in (RouteStatus.IN_PROGRESS, RouteStatus.DONE)
        ),
        "routes_completed": sum(1 for r in plan if r.status is RouteStatus.DONE),
        "routes_cancelled": sum(1 for r in plan if r.status is RouteStatus.CANCELLED),
        "stops_total": stops_total,
        "stops_done": stops_done,
        "stops_pending": sum(1 for st in all_stops if st.status is RouteStopStatus.PENDING),
        "stops_reassigned": sum(1 for st in all_stops if st.reassigned),
        "executions_total": len(execs),
        "executions_with_evidence": sum(
            1 for e in execs if e.has_evidence or e.has_manual_completion
        ),
        "executions_flagged": sum(1 for e in execs if e.flagged),
        "impediments_total": sum(1 for r in plan if r.has_impediment),
        "alerts_total": sum(1 for r in plan if r.has_alert),
        "completion_rate": stops_done / stops_total if stops_total else 0.0,
    }


_METRIC_KEYS = tuple(_expected_metrics(_PLAN))


class _Seed:
    worker_a_id: uuid.UUID
    worker_a_name: str
    worker_b_id: uuid.UUID
    worker_b_name: str


async def _seed(db: AsyncSession) -> _Seed:
    seed = _Seed()
    manager = User(
        name="Gestora PU",
        email=_MANAGER_EMAIL,
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_user = User(
        name="Operario",
        email=_WORKER_EMAIL,
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj=str(uuid.uuid4().int)[:14])
    building = Building(name="Bloco CI", campus_area="CCT")
    service_type = ServiceType(name="Varricao", average_duration_minutes=30)
    db.add_all([manager, worker_user, company, building, service_type])
    await db.flush()
    floor = Floor(building_id=building.id, label="Terreo")
    db.add(floor)
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="x", latitude=-7.2, longitude=-35.9
    )
    worker_a = FieldWorker(full_name="Ana Costa", contractor_company_id=company.id)
    worker_b = FieldWorker(full_name="Bruno Dias", contractor_company_id=company.id)
    db.add_all([point, worker_a, worker_b])
    await db.flush()
    seed.worker_a_id, seed.worker_a_name = worker_a.id, worker_a.full_name
    seed.worker_b_id, seed.worker_b_name = worker_b.id, worker_b.full_name
    worker_ids = {"a": worker_a.id, "b": worker_b.id}

    for spec in _PLAN:
        started_at = (
            datetime(
                spec.route_date.year, spec.route_date.month, spec.route_date.day, 8, tzinfo=UTC
            )
            if spec.status in (RouteStatus.IN_PROGRESS, RouteStatus.DONE)
            else None
        )
        route = Route(
            field_worker_id=worker_ids[spec.worker],
            route_date=spec.route_date,
            status=spec.status,
            started_at=started_at,
        )
        db.add(route)
        await db.flush()

        first_stop_id: uuid.UUID | None = None
        for order_index, stop_spec in enumerate(spec.stops):
            stop = RouteStop(
                route_id=route.id,
                service_point_id=point.id,
                service_type_id=service_type.id,
                order_index=order_index,
                status=stop_spec.status,
            )
            db.add(stop)
            await db.flush()
            if first_stop_id is None:
                first_stop_id = stop.id

            if stop_spec.reassigned:
                db.add_all(
                    [
                        StopAssignment(
                            route_stop_id=stop.id,
                            field_worker_id=worker_ids[spec.worker],
                            sequence=1,
                            assigned_by=manager.id,
                        ),
                        StopAssignment(
                            route_stop_id=stop.id,
                            field_worker_id=worker_ids["b"],
                            sequence=2,
                            assigned_by=manager.id,
                        ),
                    ]
                )

            exec_spec = stop_spec.execution
            if exec_spec is None:
                continue
            checked_in = datetime(
                spec.route_date.year, spec.route_date.month, spec.route_date.day, 9, tzinfo=UTC
            )
            execution = Execution(
                route_stop_id=stop.id,
                field_worker_id=worker_ids[spec.worker],
                checked_in_at=checked_in,
                checked_out_at=checked_in + timedelta(minutes=20),
                synced_at=checked_in + timedelta(minutes=25),
                source=ExecutionSource.APP,
                idempotency_key=uuid.uuid4(),
                review_status=(
                    ExecutionReviewStatus.PENDING_REVIEW
                    if exec_spec.flagged
                    else ExecutionReviewStatus.NONE
                ),
            )
            db.add(execution)
            await db.flush()
            if exec_spec.has_evidence:
                db.add(
                    EvidenceItem(
                        execution_id=execution.id,
                        kind=EvidenceKind.NOTE,
                        text_body="servico concluido",
                        captured_at=checked_in + timedelta(minutes=18),
                    )
                )
            if exec_spec.has_manual_completion:
                db.add(
                    ManualCompletion(
                        route_stop_id=stop.id,
                        execution_id=execution.id,
                        completed_by=manager.id,
                        reason="profissional sem acesso ao app",
                        completed_at=checked_in + timedelta(minutes=30),
                    )
                )

        if spec.has_impediment:
            db.add(
                Impediment(
                    route_id=route.id,
                    route_stop_id=first_stop_id,
                    scope=ImpedimentScope.STOP,
                    reason=ImpedimentReason.ACCESS_BLOCKED,
                    note="portao trancado",
                    reported_by=worker_ids[spec.worker],
                    reported_at=datetime(
                        spec.route_date.year,
                        spec.route_date.month,
                        spec.route_date.day,
                        10,
                        tzinfo=UTC,
                    ),
                )
            )
        if spec.has_alert:
            db.add(
                Alert(
                    kind=AlertKind.LATE_STOP,
                    severity=AlertSeverity.WARNING,
                    dedup_key=f"SEED:{route.id}",
                    description="stop atrasado alem da janela",
                    route_id=route.id,
                    route_stop_id=first_stop_id,
                    field_worker_id=worker_ids[spec.worker],
                )
            )

    await db.commit()
    return seed


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _compliance(
    client: TestClient, token: str, path: str = "/reports/compliance", **params: str
) -> Response:
    query = {
        "granularity": "MONTHLY",
        "date_from": _MONTH_FROM.isoformat(),
        "date_to": _MONTH_TO.isoformat(),
    }
    query.update(params)
    return client.get(path, params=query, headers=_auth(token))


@pytest.mark.slow
async def test_monthly_compliance_report_and_exports_end_to_end(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    expected = _expected_metrics(_PLAN)

    # --- MONTHLY: exactly one bucket, every metric checked against the seed ------------------
    monthly = _compliance(client, token, granularity="MONTHLY")
    assert monthly.status_code == 200, monthly.text
    body = monthly.json()
    assert body["granularity"] == "MONTHLY"
    assert len(body["periods"]) == 1
    bucket = body["periods"][0]
    assert bucket["period_label"] == "2026-09"
    assert bucket["period_start"] == "2026-09-01"
    for key in _METRIC_KEYS:
        assert bucket[key] == pytest.approx(expected[key]), key
        # One bucket → the totals row is that same row.
        assert body["totals"][key] == pytest.approx(expected[key]), key
    assert body["filters_applied"]["field_worker_name"] is None

    # --- DAILY: one bucket per seeded weekday, ascending, no weekend ------------------------
    weekdays = _weekdays(_MONTH_FROM, _MONTH_TO)
    daily = _compliance(client, token, granularity="DAILY")
    assert daily.status_code == 200, daily.text
    daily_periods = daily.json()["periods"]
    assert len(daily_periods) == len(weekdays) == 22
    starts = [p["period_start"] for p in daily_periods]
    assert starts == sorted(starts)
    assert starts == [d.isoformat() for d in weekdays]
    assert all(date.fromisoformat(s).weekday() < 5 for s in starts)
    assert [p["period_label"] for p in daily_periods] == [d.isoformat() for d in weekdays]

    # --- WEEKLY: the ISO weeks September 2026 spans — Mondays 2026-08-31..2026-09-28 --------
    mondays = sorted({d - timedelta(days=d.weekday()) for d in weekdays})
    expected_week_labels = [
        f"{m.isocalendar().year:04d}-W{m.isocalendar().week:02d}" for m in mondays
    ]
    assert expected_week_labels == ["2026-W36", "2026-W37", "2026-W38", "2026-W39", "2026-W40"]
    weekly = _compliance(client, token, granularity="WEEKLY")
    assert weekly.status_code == 200, weekly.text
    weekly_periods = weekly.json()["periods"]
    assert [p["period_label"] for p in weekly_periods] == expected_week_labels
    assert [p["period_start"] for p in weekly_periods] == [m.isoformat() for m in mondays]
    assert [p["period_start"] for p in weekly_periods] == sorted(
        p["period_start"] for p in weekly_periods
    )
    # Every weekly routes_total sums back to the month total.
    assert sum(p["routes_total"] for p in weekly_periods) == expected["routes_total"]

    # --- PDF export ------------------------------------------------------------------------
    pdf = _compliance(client, token, "/reports/compliance.pdf", granularity="MONTHLY")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    disposition = pdf.headers["content-disposition"]
    assert "attachment" in disposition
    assert "relatorio-conformidade-2026-09-01-2026-09-30.pdf" in disposition
    assert pdf.content[:4] == b"%PDF"
    assert len(pdf.content) > 1000

    # --- XLSX export ---------------------------------------------------------------------
    xlsx = _compliance(client, token, "/reports/compliance.xlsx", granularity="MONTHLY")
    assert xlsx.status_code == 200, xlsx.text
    assert xlsx.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in xlsx.headers["content-disposition"]
    assert xlsx.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(xlsx.content)) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()

    # --- Filter: narrow to one worker's routes -------------------------------------------
    worker_b_routes = sum(1 for r in _PLAN if r.worker == "b")
    assert worker_b_routes == 3
    filtered = _compliance(
        client, token, granularity="MONTHLY", field_worker_id=str(seed.worker_b_id)
    )
    assert filtered.status_code == 200, filtered.text
    filtered_body = filtered.json()
    assert len(filtered_body["periods"]) == 1
    assert filtered_body["periods"][0]["routes_total"] == worker_b_routes
    assert filtered_body["totals"]["routes_total"] == worker_b_routes
    assert filtered_body["filters_applied"]["field_worker_name"] == seed.worker_b_name
