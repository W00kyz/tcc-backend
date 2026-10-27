"""Etapa 10 — the `/reports` endpoints against the real testcontainer (spec §2 Rulings 5, 6),
counting on the point-by-day model (spec 2026-10-20-alocacao-automatica §8.7).

A tiny fixed scenario: two typed points over the 22 service days of September 2026; worker A
does one of them on Thu 09-03, worker B leaves the campus holding it on Fri 09-04, and A wins an
appeal on the other one on Tue 09-08 — so the `field_worker_id` and `outcome` filters have
something to narrow. The JSON endpoint's shape and a couple of
metric values are asserted against the seed; the exports are checked for their content type,
attachment disposition and magic bytes; and every param-validation branch plus the role gate is
exercised.
"""

import uuid
from datetime import UTC, date, datetime

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
_MANAGER_EMAIL = "gestora@ufcg.edu.br"
_WORKER_EMAIL = "operario@empresa.com"
_FROM = date(2026, 9, 1)
_TO = date(2026, 9, 30)
_SERVICE_DAYS = 22


class _Seed:
    worker_a: uuid.UUID
    worker_b: uuid.UUID
    company: uuid.UUID


async def _seed(db: AsyncSession) -> _Seed:
    seed = _Seed()
    db.add(
        User(
            name="Gestora",
            email=_MANAGER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    service_type = await make_service_type(db, company_name="Limpa Tudo", type_name="Varrição")
    company = service_type.contractor_company
    assert company is not None
    points = await make_points(db, service_type=service_type, names=["Sala 101", "Sala 102"])
    for point in points:
        point.created_at = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    worker_a = await make_worker(db, company=company, full_name="Ana Costa", email=_WORKER_EMAIL)
    worker_b = await make_worker(db, company=company, full_name="Bruno Dias")
    seed.worker_a, seed.worker_b, seed.company = worker_a.id, worker_b.id, company.id

    done = await make_route(db, worker=worker_a, day=date(2026, 9, 3), points=[points[0]])
    done.stops[0].status = RouteStopStatus.DONE
    released = await make_route(db, worker=worker_b, day=date(2026, 9, 4), points=[points[0]])
    released.stops[0].status = RouteStopStatus.RELEASED
    released.stops[0].release_reason = ReleaseReason.LEFT_CAMPUS
    released.stops[0].released_at = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
    # Tue 09-08: A goes silent holding the other point and wins the appeal.
    appealed = await make_route(db, worker=worker_a, day=date(2026, 9, 8), points=[points[1]])
    appealed.stops[0].status = RouteStopStatus.RELEASED
    appealed.stops[0].release_reason = ReleaseReason.LOCATION_SILENCE
    appealed.stops[0].released_at = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    db.add(
        Appeal(
            route_stop_id=appealed.stops[0].id,
            field_worker_id=worker_a.id,
            reason=AppealReason.APP_CLOSED,
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


def _get(client: TestClient, token: str, path: str, **params: str) -> Response:
    query: dict[str, str] = {
        "granularity": "DAILY",
        "date_from": _FROM.isoformat(),
        "date_to": _TO.isoformat(),
    }
    query.update(params)
    return client.get(path, params=query, headers=_auth(token))


async def test_compliance_json_happy_path(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["granularity"] == "DAILY"
    assert {"granularity", "periods", "totals", "filters_applied", "generated_at"} <= body.keys()
    starts = [p["period_start"] for p in body["periods"]]
    assert len(starts) == _SERVICE_DAYS
    assert "2026-09-05" not in starts  # a Saturday
    assert body["totals"]["points_total"] == 2 * _SERVICE_DAYS
    assert body["totals"]["points_done"] == 2
    assert body["totals"]["points_done_by_appeal"] == 1
    assert body["totals"]["points_unserved"] == 2 * _SERVICE_DAYS - 2
    assert body["totals"]["points_released"] == 2
    assert body["totals"]["released_left_campus"] == 1
    assert body["totals"]["executions_total"] == 0
    assert body["totals"]["alerts_total"] == 0
    assert body["totals"]["completion_rate"] == 2 / (2 * _SERVICE_DAYS)
    assert not {"routes_total", "stops_reassigned", "routes_cancelled"} & body["totals"].keys()


@pytest.mark.parametrize(
    ("outcome", "label", "days"),
    [
        ("DONE", "Feito", ["2026-09-03"]),
        ("DONE_BY_APPEAL", "Feito por recurso", ["2026-09-08"]),
        ("UNSERVED", "Não atendido", None),
    ],
)
async def test_outcome_filter_narrows_to_one_outcome(
    client: TestClient,
    db_session: AsyncSession,
    outcome: str,
    label: str,
    days: list[str] | None,
) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance", outcome=outcome)

    assert response.status_code == 200, response.text
    body = response.json()
    expected_total = len(days) if days is not None else 2 * _SERVICE_DAYS - 2
    assert body["totals"]["points_total"] == expected_total
    if days is not None:
        assert [p["period_start"] for p in body["periods"]] == days
    assert body["filters_applied"]["outcome"] == label


async def test_bad_outcome_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    for path in ("/reports/compliance", "/reports/compliance.pdf", "/reports/compliance.xlsx"):
        response = _get(client, token, path, outcome="BOGUS")
        assert response.status_code == 422, (path, response.text)


async def test_field_worker_filter_narrows_metrics(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance", field_worker_id=str(seed.worker_b))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [p["period_start"] for p in body["periods"]] == ["2026-09-04"]
    assert body["totals"]["points_total"] == 1
    assert body["totals"]["points_done"] == 0
    assert body["totals"]["points_released"] == 1
    assert body["filters_applied"]["field_worker_name"] == "Bruno Dias"


async def test_company_filter_echoes_its_name(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance", company_id=str(seed.company))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["points_total"] == 2 * _SERVICE_DAYS
    assert body["filters_applied"]["company_name"] == "Limpa Tudo"
    assert body["filters_applied"]["outcome"] is None


async def test_inverted_range_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    response = _get(
        client, token, "/reports/compliance", date_from="2026-09-30", date_to="2026-09-01"
    )
    assert response.status_code == 422, response.text


async def test_span_over_366_days_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    response = _get(
        client, token, "/reports/compliance", date_from="2025-01-01", date_to="2026-06-01"
    )
    assert response.status_code == 422, response.text


async def test_bad_granularity_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    response = _get(client, token, "/reports/compliance", granularity="BOGUS")
    assert response.status_code == 422, response.text


async def test_field_worker_token_is_403_on_all_three(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)
    for path in ("/reports/compliance", "/reports/compliance.pdf", "/reports/compliance.xlsx"):
        assert _get(client, token, path).status_code == 403, path


async def test_compliance_pdf_export(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance.pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".pdf" in disposition
    assert "2026-09-01-2026-09-30" in disposition
    assert response.content[:4] == b"%PDF"


async def test_compliance_xlsx_export(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance.xlsx")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert response.content[:2] == b"PK"
