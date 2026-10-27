"""Cenário de relatórios ponta a ponta — relatório mensal de conformidade + exportação
(requisitos §7 RF43-RF46, design §2 Ruling 9 e §7), na base ponto-dia de serviço (spec
2026-10-20-alocacao-automatica §8.7).

O deliverable da Etapa 10: um mês real de trabalho acumulado (três pontos tipados, todo dia útil
de setembro/2026) com pontos feitos, liberados por vários motivos, impedidos e um recurso aceito
é consolidado por `GET /reports/compliance` nas três granularidades e exportado em PDF e XLSX.
Cada hop roda contra o Postgres/PostGIS real via uma requisição HTTP de verdade (`TestClient`),
autenticada como gestora.

O que fica pinado aqui:
- os indicadores + `completion_rate` de um bucket MENSAL, conferidos contra valores
  recomputados no teste a partir das constantes do seed (`_PLAN`), nunca lidos de volta da
  resposta;
- a linha de TOTAIS igual ao único bucket mensal;
- os buckets DIÁRIO (um por dia útil, sem fim de semana, crescentes) e SEMANAL (as semanas ISO
  que setembro/2026 abrange — 2026-W36..W40);
- o PDF (`%PDF`, > 1 KB, `application/pdf`, `Content-Disposition` de anexo) e o XLSX (`PK`,
  contém `xl/worksheets/sheet1.xml`, content-type de planilha);
- o filtro `field_worker_id` estreitando para os pontos-dia que um profissional teve no lote.

O que fica como passo manual do autor (mesmo tratamento de `test_scenario_5_alerts.py`):
- baixar os dois arquivos pelo painel com dados reais do piloto e conferir o layout num leitor
  de PDF / editor de planilha de verdade.

Estratégia de seed: **inserção direta** via os modelos de domínio. Montar um mês pelos serviços
reais (alocação + check-in/check-out por HTTP) seria dezenas de requisições e um fixture gigante
para zero cobertura extra do read model — que é o alvo. Rotas, paradas e recursos são inseridos
direto; o único caminho por serviço real exercitado aqui é o endpoint `/reports/compliance` sob
teste.

Marcado `slow`, fora do run padrão (`-m "not slow"` no `addopts`); selecionar com
`uv run pytest -m slow`.
"""

import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from app.core.security import hash_password
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import ReleaseReason, RouteStopStatus
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

pytestmark = pytest.mark.asyncio

_PASSWORD = "senha-forte-o-suficiente"
_MANAGER_EMAIL = "gestora@pu.ufcg.edu.br"
_MONTH_FROM = date(2026, 9, 1)
_MONTH_TO = date(2026, 9, 30)
_POINTS = 3

# --- Seed constants — the whole scenario is derived from these so the assertions can recompute
# every expected metric instead of trusting the response. -----------------------------------
_WORKER_B_DAY_INDEXES = frozenset({3, 11, 19})  # dias do segundo profissional
_APPEALED_DAY_INDEX = 5  # P1 liberado por silêncio, recurso aceito
_IMPEDED_DAY_INDEX = 4  # P2 impedido
_END_OF_DAY_INDEX = 9  # P2 ainda no lote quando o expediente acabou


@dataclass(frozen=True)
class _StopSpec:
    point: int
    status: RouteStopStatus
    reason: ReleaseReason | None = None
    appealed: bool = False


@dataclass(frozen=True)
class _DaySpec:
    index: int
    day: date
    worker: str  # "a" | "b"
    stops: tuple[_StopSpec, ...]


def _weekdays(month_from: date, month_to: date) -> list[date]:
    days = (month_to - month_from).days + 1
    return [
        d
        for d in (month_from + timedelta(days=i) for i in range(days))
        if d.weekday() < 5  # segunda a sexta
    ]


def _day_stops(i: int) -> tuple[_StopSpec, ...]:
    """P0 feito todo dia; P1 feito nos dias pares e liberado (saiu do campus) nos ímpares, menos
    o do recurso; P2 fica no pool, menos no dia do impedimento e no do fim do expediente."""
    done, released = RouteStopStatus.DONE, RouteStopStatus.RELEASED
    stops = [_StopSpec(0, done)]
    if i == _APPEALED_DAY_INDEX:
        stops.append(_StopSpec(1, released, ReleaseReason.LOCATION_SILENCE, appealed=True))
    elif i % 2:
        stops.append(_StopSpec(1, released, ReleaseReason.LEFT_CAMPUS))
    else:
        stops.append(_StopSpec(1, done))
    if i == _IMPEDED_DAY_INDEX:
        stops.append(_StopSpec(2, released, ReleaseReason.IMPEDIMENT))
    if i == _END_OF_DAY_INDEX:
        stops.append(_StopSpec(2, released, ReleaseReason.END_OF_DAY))
    return tuple(stops)


def _build_plan() -> list[_DaySpec]:
    """One route per weekday of September 2026, varied so no metric is trivially uniform."""
    return [
        _DaySpec(i, day, "b" if i in _WORKER_B_DAY_INDEXES else "a", _day_stops(i))
        for i, day in enumerate(_weekdays(_MONTH_FROM, _MONTH_TO))
    ]


_PLAN = _build_plan()


# --- Expected metrics, recomputed straight from `_PLAN` -------------------------------------
def _expected_metrics(plan: list[_DaySpec]) -> dict[str, int | float]:
    stops = [stop for day in plan for stop in day.stops]
    total = len(plan) * _POINTS
    done = sum(1 for st in stops if st.status is RouteStopStatus.DONE or st.appealed)

    def released(reason: ReleaseReason) -> int:
        return sum(1 for st in stops if st.reason is reason)

    return {
        "points_total": total,
        "points_done": done,
        "points_done_by_appeal": sum(1 for st in stops if st.appealed),
        "points_unserved": total - done,
        "points_released": sum(1 for st in stops if st.reason is not None),
        "points_impeded": released(ReleaseReason.IMPEDIMENT),
        "released_route_remainder": released(ReleaseReason.ROUTE_REMAINDER),
        "released_left_campus": released(ReleaseReason.LEFT_CAMPUS),
        "released_location_silence": released(ReleaseReason.LOCATION_SILENCE),
        "released_end_of_day": released(ReleaseReason.END_OF_DAY),
        "completion_rate": done / total,
    }


_METRIC_KEYS = tuple(_expected_metrics(_PLAN))


class _Seed:
    worker_b_id: uuid.UUID
    worker_b_name: str


async def _seed(db: AsyncSession) -> _Seed:
    seed = _Seed()
    db.add(
        User(
            name="Gestora PU",
            email=_MANAGER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    service_type = await make_service_type(db, company_name="Limpa Tudo", type_name="Varricao")
    company = service_type.contractor_company
    assert company is not None
    points = await make_points(
        db, service_type=service_type, names=[f"Sala 10{i}" for i in range(_POINTS)]
    )
    for point in points:
        point.created_at = datetime(2026, 1, 5, 12, tzinfo=UTC)
    workers = {
        "a": await make_worker(db, company=company, full_name="Ana Costa"),
        "b": await make_worker(db, company=company, full_name="Bruno Dias"),
    }
    seed.worker_b_id, seed.worker_b_name = workers["b"].id, workers["b"].full_name

    for spec in _PLAN:
        route = await make_route(
            db,
            worker=workers[spec.worker],
            day=spec.day,
            points=[points[stop.point] for stop in spec.stops],
        )
        for stop, stop_spec in zip(route.stops, spec.stops, strict=True):
            stop.status = stop_spec.status
            if stop_spec.reason is not None:
                stop.release_reason = stop_spec.reason
                stop.released_at = datetime(
                    spec.day.year, spec.day.month, spec.day.day, 15, tzinfo=UTC
                )
            if stop_spec.appealed:
                db.add(
                    Appeal(
                        route_stop_id=stop.id,
                        field_worker_id=workers[spec.worker].id,
                        reason=AppealReason.NO_SIGNAL,
                        status=AppealStatus.ACCEPTED,
                        idempotency_key=uuid.uuid4(),
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
    # Every weekly points_total sums back to the month total.
    assert sum(p["points_total"] for p in weekly_periods) == expected["points_total"]

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

    # --- Filter: narrow to the point-days one worker held ---------------------------------
    worker_b_point_days = sum(len(d.stops) for d in _PLAN if d.worker == "b")
    assert worker_b_point_days == 6
    filtered = _compliance(
        client, token, granularity="MONTHLY", field_worker_id=str(seed.worker_b_id)
    )
    assert filtered.status_code == 200, filtered.text
    filtered_body = filtered.json()
    assert len(filtered_body["periods"]) == 1
    assert filtered_body["periods"][0]["points_total"] == worker_b_point_days
    assert filtered_body["totals"]["points_total"] == worker_b_point_days
    assert filtered_body["filters_applied"]["field_worker_name"] == seed.worker_b_name
