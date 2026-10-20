"""The allocation HTTP surface (spec 2026-10-20-alocacao-automatica §6.2): `POST /routes/start`,
`POST /routes/{id}/next-batch` and the worker's day at `GET /routes/me`. The batch mechanics are
covered in tests/domain/routing/test_allocation.py; here we pin status codes and bodies."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from app.api.deps import get_now
from app.core.campus_clock import campus_today
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.privacy.service import record_consent
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from app.domain.settings.service import get_settings_row
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_route, make_worker, seed_privacy_notice
from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"
# Tuesday 10:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
_SATURDAY = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)
_FIVE_PM = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
_ON_CAMPUS = {"latitude": -7.2150, "longitude": -35.9100}
_OFF_CAMPUS = {"latitude": -7.2300, "longitude": -35.9100}


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    # Local override so nothing reaches a real OSRM (spec §8).
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _freeze(client: TestClient, now: datetime) -> None:
    cast(FastAPI, client.app).dependency_overrides[get_now] = lambda: now


@dataclass
class _Seeded:
    worker: FieldWorker
    token: str
    points: list[ServicePoint]


async def _seed(
    db: AsyncSession, client: TestClient, *, rooms: int = 3, consented: bool = True
) -> _Seeded:
    """An approved worker with a login and consent, and `rooms` points of their company's type
    in one building inside the campus. Batch size 2."""
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([company, building])
    await db.flush()
    service_type = ServiceType(
        name="Limpeza", average_duration_minutes=30, contractor_company_id=company.id
    )
    floor = Floor(building_id=building.id, label="Térreo")
    db.add_all([service_type, floor])
    await db.flush()
    points = [
        ServicePoint(
            floor_id=floor.id,
            name=f"Sala {i}",
            description="Sala",
            latitude=-7.2150,
            longitude=-35.9095,
            service_type_id=service_type.id,
        )
        for i in range(1, rooms + 1)
    ]
    db.add_all(points)
    worker = await make_worker(db, company=company, email="joao@empresa.com")
    (await get_settings_row(db)).allocation_batch_size = 2
    await seed_privacy_notice(db)
    if consented:
        await record_consent(db, user_id=worker.user_id, ip_address=None, user_agent=None)
    await db.commit()
    _freeze(client, _NOW)
    return _Seeded(worker=worker, token=_login(client, "joao@empresa.com"), points=points)


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _post(
    client: TestClient, token: str, path: str, position: dict[str, float] = _ON_CAMPUS
) -> Response:
    body = {**position, "captured_at": _NOW.isoformat()}
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def _my_day(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get("/routes/me", headers={"Authorization": f"Bearer {token}"})
    return cast(dict[str, Any], response.json())


async def _finish_batch(db: AsyncSession, route_id: str) -> None:
    await db.execute(
        update(RouteStop).where(RouteStop.route_id == route_id).values(status=RouteStopStatus.DONE)
    )
    await db.commit()


# --- POST /routes/start ----------------------------------------------------------------------


async def test_start_returns_the_first_batch(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session, client)

    response = _post(client, seeded.token, "/routes/start")

    assert response.status_code == 200
    body = response.json()
    assert body["pool_empty"] is False
    assert body["route"]["status"] == "IN_PROGRESS"
    assert body["route"]["route_date"] == campus_today(_NOW).isoformat()
    assert body["route"]["current_batch"] == 1
    assert [stop["service_point_name"] for stop in body["route"]["stops"]] == ["Sala 1", "Sala 2"]


@pytest.mark.parametrize(
    ("now", "position", "code"),
    [
        (_SATURDAY, _ON_CAMPUS, "NOT_A_SERVICE_DAY"),
        (_FIVE_PM, _ON_CAMPUS, "WORKDAY_OVER"),
        (_NOW, _OFF_CAMPUS, "OUTSIDE_CAMPUS"),
    ],
)
async def test_start_refusals_leave_no_route(
    client: TestClient,
    db_session: AsyncSession,
    now: datetime,
    position: dict[str, float],
    code: str,
) -> None:
    seeded = await _seed(db_session, client)
    _freeze(client, now)

    response = _post(client, seeded.token, "/routes/start", position)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert response.json()["detail"]["detail"]
    assert await db_session.scalar(select(Route.id)) is None


async def test_start_on_a_done_route_is_route_done(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    await make_route(
        db_session,
        worker=seeded.worker,
        day=campus_today(_NOW),
        points=[],
        status=RouteStatus.DONE,
    )
    await db_session.commit()

    response = _post(client, seeded.token, "/routes/start")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ROUTE_DONE"


async def test_start_requires_consent(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session, client, consented=False)

    response = _post(client, seeded.token, "/routes/start")

    assert response.status_code == 403
    assert await db_session.scalar(select(Route.id)) is None


async def test_start_refuses_a_worker_not_approved(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    seeded.worker.status = FieldWorkerStatus.PENDING_APPROVAL
    await db_session.commit()

    response = _post(client, seeded.token, "/routes/start")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "WORKER_NOT_APPROVED"


# --- POST /routes/{id}/next-batch ------------------------------------------------------------


async def test_next_batch_after_the_batch_is_done(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    route_id = _post(client, seeded.token, "/routes/start").json()["route"]["id"]
    await _finish_batch(db_session, route_id)

    response = _post(client, seeded.token, f"/routes/{route_id}/next-batch")

    assert response.status_code == 200
    body = response.json()
    assert body["pool_empty"] is False
    assert body["route"]["current_batch"] == 2
    assert len(body["route"]["stops"]) == 3  # both batches; the app shows the current one


async def test_next_batch_with_an_empty_pool_closes_the_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client, rooms=2)
    route_id = _post(client, seeded.token, "/routes/start").json()["route"]["id"]
    await _finish_batch(db_session, route_id)

    response = _post(client, seeded.token, f"/routes/{route_id}/next-batch")

    assert response.status_code == 200
    assert response.json()["pool_empty"] is True
    assert response.json()["route"]["status"] == "DONE"
    again = _post(client, seeded.token, f"/routes/{route_id}/next-batch")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ROUTE_DONE"


async def test_next_batch_outside_campus_is_refused(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    route_id = _post(client, seeded.token, "/routes/start").json()["route"]["id"]

    response = _post(client, seeded.token, f"/routes/{route_id}/next-batch", _OFF_CAMPUS)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "OUTSIDE_CAMPUS"


async def test_next_batch_of_another_workers_route_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    company = await db_session.get_one(ContractorCompany, seeded.worker.contractor_company_id)
    colleague = await make_worker(db_session, company=company)
    other = await make_route(db_session, worker=colleague, day=campus_today(_NOW), points=[])
    await db_session.commit()

    response = _post(client, seeded.token, f"/routes/{other.id}/next-batch")

    assert response.status_code == 404


# --- GET /routes/me --------------------------------------------------------------------------


async def test_my_day_without_a_route(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session, client)

    assert _my_day(client, seeded.token) == {
        "service_day": True,
        "route": None,
        "position_report_interval_seconds": 60,
        "location_silence_warning_minutes": 30,
        "location_silence_release_minutes": 45,
    }


async def test_my_day_with_a_route(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session, client)
    route_id = _post(client, seeded.token, "/routes/start").json()["route"]["id"]

    body = _my_day(client, seeded.token)

    assert body["service_day"] is True
    assert body["route"]["id"] == route_id
    assert body["route"]["current_batch"] == 1


async def test_my_day_on_a_saturday_is_not_a_service_day(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session, client)
    _freeze(client, _SATURDAY)

    body = _my_day(client, seeded.token)

    assert body["service_day"] is False
    assert body["route"] is None
