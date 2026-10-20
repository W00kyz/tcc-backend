"""Etapa 10 — the `/reports` endpoints against the real testcontainer (spec §2 Rulings 5, 6).

A tiny fixed scenario: two routes over two days for one field worker, plus a second worker with
one route, so the `field_worker_id` filter has something to narrow. The JSON endpoint's shape
and a couple of metric values are asserted against the seed; the exports are checked for their
content type, attachment disposition and magic bytes; and every param-validation branch plus the
role gate is exercised.
"""

import uuid
from datetime import date

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_worker

pytestmark = pytest.mark.asyncio

_PASSWORD = "senha-forte-o-suficiente"
_MANAGER_EMAIL = "gestora@ufcg.edu.br"
_WORKER_EMAIL = "operario@empresa.com"
_FROM = date(2026, 9, 1)
_TO = date(2026, 9, 30)


class _Seed:
    worker_a: uuid.UUID
    worker_b: uuid.UUID


async def _seed(db: AsyncSession) -> _Seed:
    seed = _Seed()
    manager = User(
        name="Gestora",
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
    service_type = ServiceType(name="Varrição", average_duration_minutes=30)
    db.add_all([manager, worker_user, company, building, service_type])
    await db.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="x", latitude=-7.2, longitude=-35.9
    )
    other_point = ServicePoint(
        floor_id=floor.id, name="Sala 102", description="x", latitude=-7.2, longitude=-35.9
    )
    worker_a = await make_worker(db, company=company, full_name="Ana Costa")
    worker_b = await make_worker(db, company=company, full_name="Bruno Dias")
    db.add_all([point, other_point, worker_a, worker_b])
    await db.flush()
    seed.worker_a = worker_a.id
    seed.worker_b = worker_b.id

    # R1 — worker A, 2026-09-03, DONE, two stops (one done, one pending).
    r1 = Route(field_worker_id=worker_a.id, route_date=date(2026, 9, 3), status=RouteStatus.DONE)
    db.add(r1)
    await db.flush()
    db.add_all(
        [
            RouteStop(
                route_id=r1.id,
                service_point_id=point.id,
                service_type_id=service_type.id,
                order_index=0,
                batch_number=1,
                service_date=r1.route_date,
                status=RouteStopStatus.DONE,
            ),
            RouteStop(
                route_id=r1.id,
                service_point_id=other_point.id,
                service_type_id=service_type.id,
                order_index=1,
                batch_number=1,
                service_date=r1.route_date,
                status=RouteStopStatus.PENDING,
            ),
        ]
    )

    # R2 — worker A, 2026-09-04, PAUSED, no stops.
    db.add(
        Route(
            field_worker_id=worker_a.id,
            route_date=date(2026, 9, 4),
            status=RouteStatus.PAUSED,
        )
    )

    # R3 — worker B, 2026-09-05, IN_PROGRESS, no stops.
    db.add(
        Route(
            field_worker_id=worker_b.id,
            route_date=date(2026, 9, 5),
            status=RouteStatus.IN_PROGRESS,
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
    assert starts == ["2026-09-03", "2026-09-04", "2026-09-05"]
    assert body["totals"]["routes_total"] == 3
    assert body["totals"]["routes_completed"] == 1
    assert body["totals"]["stops_total"] == 2
    assert body["totals"]["stops_done"] == 1
    assert body["totals"]["completion_rate"] == 0.5


async def test_field_worker_filter_narrows_metrics(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)

    response = _get(client, token, "/reports/compliance", field_worker_id=str(seed.worker_b))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [p["period_start"] for p in body["periods"]] == ["2026-09-05"]
    assert body["totals"]["routes_total"] == 1
    assert body["totals"]["stops_total"] == 0
    assert body["filters_applied"]["field_worker_name"] == "Bruno Dias"


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


async def test_bad_route_status_is_422(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    token = _login(client, _MANAGER_EMAIL)
    response = _get(client, token, "/reports/compliance", route_status="BOGUS")
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
