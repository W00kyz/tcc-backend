"""Etapa 9 — the monitoring board HTTP surface. The derived `is_delayed`, the OPEN-alert
count and the latest-position-per-worker fold are pinned here against a real testcontainer;
RBAC too."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertSeverity,
    AlertStatus,
    WorkerPosition,
)
from app.domain.routing.models import Route, RouteStatus, RouteStopStatus, RouteType
from app.domain.routing.service import StopInput, create_route
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"
_TODAY = date.today()


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Fixture:
    def __init__(self, *, manager: User, worker_user: User, worker: FieldWorker) -> None:
        self.manager = manager
        self.worker_user = worker_user
        self.worker = worker


async def _seed_actors(db: AsyncSession) -> _Fixture:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_user = User(
        name="Joao",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    db.add_all([manager, worker_user, company])
    await db.flush()
    worker = FieldWorker(
        full_name="Joao da Silva", contractor_company_id=company.id, user_id=worker_user.id
    )
    db.add(worker)
    await db.flush()
    return _Fixture(manager=manager, worker_user=worker_user, worker=worker)


async def _points(db: AsyncSession, count: int) -> list[ServicePoint]:
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add(building)
    await db.flush()
    floor = Floor(building_id=building.id, label="Terreo")
    db.add(floor)
    await db.flush()
    points = [
        ServicePoint(
            floor_id=floor.id,
            name=f"Ponto {i}",
            description="Sala",
            latitude=-7.20 - i * 0.01,
            longitude=-35.90 - i * 0.01,
        )
        for i in range(count)
    ]
    db.add_all(points)
    await db.flush()
    return points


async def _make_route(
    db: AsyncSession,
    fx: _Fixture,
    *,
    point_count: int = 1,
    status: RouteStatus = RouteStatus.PLANNED,
    route_date: date = _TODAY,
    scheduled_start_at: datetime | None = None,
) -> Route:
    points = await _points(db, point_count)
    route = await create_route(
        db,
        field_worker_id=fx.worker.id,
        route_date=route_date,
        route_type=RouteType.REGULAR,
        scheduled_start_at=scheduled_start_at,
        stops=[StopInput(service_point_id=p.id) for p in points],
        actor_id=fx.manager.id,
        osrm=FakeOsrmClient(),
    )
    route.status = status
    await db.flush()
    return route


def _get_board(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get("/monitoring/board", headers=_auth(token))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- empty state ----------------------------------------------------------------------


async def test_empty_board(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert body["routes"] == []
    assert body["open_alerts"] == []
    assert body["worker_positions"] == []
    assert body["generated_at"] is not None


# --- is_delayed derivation ----------------------------------------------------------------


async def test_planned_route_past_scheduled_start_is_delayed(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(
        db_session,
        fx,
        status=RouteStatus.PLANNED,
        scheduled_start_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert len(body["routes"]) == 1
    assert body["routes"][0]["route_id"] == str(route.id)
    assert body["routes"][0]["is_delayed"] is True
    assert body["routes"][0]["open_alert_count"] == 0


async def test_pending_stop_past_arrival_window_is_delayed(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    stop = route.stops[0]
    stop.expected_arrival_to = datetime.now(UTC) - timedelta(minutes=30)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert body["routes"][0]["is_delayed"] is True


async def test_pending_stop_within_arrival_window_is_not_delayed(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    route.stops[0].expected_arrival_to = datetime.now(UTC) + timedelta(hours=2)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert body["routes"][0]["is_delayed"] is False


async def test_open_late_stop_alert_marks_route_delayed_and_counts(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    alert = Alert(
        kind=AlertKind.LATE_STOP,
        severity=AlertSeverity.WARNING,
        dedup_key=f"LATE_STOP:{route.stops[0].id}",
        description="Ponto atrasado",
        route_id=route.id,
        route_stop_id=route.stops[0].id,
        field_worker_id=fx.worker.id,
        status=AlertStatus.OPEN,
    )
    db_session.add(alert)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert body["routes"][0]["is_delayed"] is True
    assert body["routes"][0]["open_alert_count"] == 1
    assert len(body["open_alerts"]) == 1
    assert body["open_alerts"][0]["kind"] == "LATE_STOP"
    assert body["open_alerts"][0]["route_id"] == str(route.id)


async def test_done_and_cancelled_routes_are_absent(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    await _make_route(db_session, fx, status=RouteStatus.DONE)
    await _make_route(db_session, fx, status=RouteStatus.CANCELLED)
    kept = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert [r["route_id"] for r in body["routes"]] == [str(kept.id)]


async def test_stop_counts_reflect_done_stops(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, point_count=3, status=RouteStatus.IN_PROGRESS)
    route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    assert body["routes"][0]["stop_total"] == 3
    assert body["routes"][0]["stop_done"] == 1


# --- worker positions -------------------------------------------------------------------


async def test_only_latest_position_per_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    old = WorkerPosition(
        field_worker_id=fx.worker.id,
        route_id=route.id,
        latitude=-7.1,
        longitude=-35.8,
        reported_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    latest = WorkerPosition(
        field_worker_id=fx.worker.id,
        route_id=route.id,
        latitude=-7.9,
        longitude=-35.1,
        reported_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    db_session.add_all([old, latest])
    await db_session.commit()
    token = _login(client, fx.manager.email)

    body = _get_board(client, token)

    positions = list(body["worker_positions"])
    assert len(positions) == 1
    assert positions[0]["latitude"] == -7.9
    assert positions[0]["field_worker_name"] == "Joao da Silva"
    assert 0 <= positions[0]["age_seconds"] <= 120


# --- RBAC -----------------------------------------------------------------------------


async def test_field_worker_forbidden(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.worker_user.email)

    response = client.get("/monitoring/board", headers=_auth(token))

    assert response.status_code == 403
