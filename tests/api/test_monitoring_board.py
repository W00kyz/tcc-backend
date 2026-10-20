"""The board on the point-by-day model (spec 2026-10-20-alocacao-automatica §8.2): per-type pool
KPIs of today, who has not started yet, today's releases, each route's current batch, and the
day's route-less alerts — with the clock pinned to a Tuesday on campus."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from app.api.deps import get_now
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.appeals.models import Appeal, AppealReason, AppealStatus
from app.domain.catalog.models import FieldWorkerStatus
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity, AlertStatus
from app.domain.routing.models import ReleaseReason, RouteStatus, RouteStopStatus
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker
from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"
_MANAGER_EMAIL = "larissa@pu.ufcg.edu.br"
# Tuesday 11:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
_TODAY = date(2026, 10, 20)
_SATURDAY = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


def _pin(client: TestClient, now: datetime) -> None:
    cast(FastAPI, client.app).dependency_overrides[get_now] = lambda: now


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        _pin(test_client, _NOW)
        yield test_client


async def _manager(db: AsyncSession) -> None:
    db.add(
        User(
            name="Larissa",
            email=_MANAGER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    await db.flush()


def _board(client: TestClient) -> dict[str, Any]:
    login = client.post("/auth/login", json={"email": _MANAGER_EMAIL, "password": _PASSWORD})
    token = login.json()["access_token"]
    response = client.get("/monitoring/board", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_pool_kpis_per_type(client: TestClient, db_session: AsyncSession) -> None:
    await _manager(db_session)
    cleaning = await make_service_type(db_session, company_name="Limpa Tudo", type_name="Limpeza")
    garden = await make_service_type(db_session, company_name="Verde", type_name="Jardinagem")
    done, appealed, pending, released, _free = await make_points(
        db_session, service_type=cleaning, names=["Feito", "Recurso", "Lote", "Liberado", "Livre"]
    )
    await make_points(db_session, service_type=garden, names=["Canteiro"])
    await make_points(db_session, service_type=None, names=["Sem tipo"])
    assert cleaning.contractor_company is not None
    ana = await make_worker(db_session, company=cleaning.contractor_company, full_name="Ana")
    route = await make_route(
        db_session, worker=ana, day=_TODAY, points=[done, appealed, pending, released]
    )
    done_stop, appealed_stop, _pending_stop, released_stop = route.stops
    done_stop.status = RouteStopStatus.DONE
    for stop in (appealed_stop, released_stop):
        stop.status = RouteStopStatus.RELEASED
        stop.release_reason = ReleaseReason.LEFT_CAMPUS
        stop.released_at = _NOW - timedelta(minutes=5)
    db_session.add(
        Appeal(
            route_stop_id=appealed_stop.id,
            field_worker_id=ana.id,
            reason=AppealReason.NO_SIGNAL,
            status=AppealStatus.ACCEPTED,
            idempotency_key=uuid.uuid4(),
        )
    )
    await db_session.commit()

    body = _board(client)

    assert body["service_day"] is True
    assert body["pool"] == [
        {
            "service_type_id": str(cleaning.id),
            "service_type_name": "Limpeza",
            "company_name": "Limpa Tudo",
            "total": 5,
            "done": 2,
            "in_progress": 1,
            "free": 2,
        },
        {
            "service_type_id": str(garden.id),
            "service_type_name": "Jardinagem",
            "company_name": "Verde",
            "total": 1,
            "done": 0,
            "in_progress": 0,
            "free": 1,
        },
    ]
    assert body["released_today"] == 2


async def test_not_started_lists_approved_workers_without_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _manager(db_session)
    service_type = await make_service_type(db_session, company_name="Limpa Tudo")
    (point,) = await make_points(db_session, service_type=service_type, names=["Sala 1"])
    company = service_type.contractor_company
    assert company is not None
    started = await make_worker(db_session, company=company, full_name="Ana")
    await make_route(db_session, worker=started, day=_TODAY, points=[point])
    idle = await make_worker(db_session, company=company, full_name="Bruno")
    await make_worker(
        db_session, company=company, full_name="Carla", status=FieldWorkerStatus.PENDING_APPROVAL
    )
    await make_worker(db_session, company=company, full_name="Davi", is_active=False)
    yesterday = await make_worker(db_session, company=company, full_name="Elisa")
    await make_route(db_session, worker=yesterday, day=_TODAY - timedelta(days=1), points=[point])
    await db_session.commit()

    body = _board(client)

    assert body["not_started"] == [
        {"id": str(idle.id), "full_name": "Bruno", "company_name": "Limpa Tudo"},
        {"id": str(yesterday.id), "full_name": "Elisa", "company_name": "Limpa Tudo"},
    ]


async def test_non_service_day_has_no_pool_and_nobody_to_start(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _manager(db_session)
    service_type = await make_service_type(db_session)
    await make_points(db_session, service_type=service_type, names=["Sala 1"])
    assert service_type.contractor_company is not None
    await make_worker(db_session, company=service_type.contractor_company)
    await db_session.commit()
    _pin(client, _SATURDAY)

    body = _board(client)

    assert body["service_day"] is False
    assert body["pool"] == []
    assert body["not_started"] == []


async def test_route_shows_current_batch_live_stops_and_done_today(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _manager(db_session)
    service_type = await make_service_type(db_session, company_name="Limpa Tudo")
    points = await make_points(
        db_session, service_type=service_type, names=["A", "B", "C", "D", "E"]
    )
    assert service_type.contractor_company is not None
    ana = await make_worker(db_session, company=service_type.contractor_company, full_name="Ana")
    route = await make_route(db_session, worker=ana, day=_TODAY, points=points)
    statuses = [
        (1, RouteStopStatus.DONE),
        (1, RouteStopStatus.DONE),
        (2, RouteStopStatus.IN_PROGRESS),
        (2, RouteStopStatus.PENDING),
        (2, RouteStopStatus.RELEASED),
    ]
    for stop, (batch, status) in zip(route.stops, statuses, strict=True):
        stop.batch_number = batch
        stop.status = status
    route.stops[4].release_reason = ReleaseReason.IMPEDIMENT
    route.stops[4].released_at = _NOW
    await db_session.commit()

    (board_route,) = _board(client)["routes"]

    assert board_route["field_worker_name"] == "Ana"
    assert board_route["company_name"] == "Limpa Tudo"
    assert board_route["status"] == "IN_PROGRESS"
    assert board_route["current_batch"] == 2
    assert board_route["done_today"] == 2
    assert [(s["service_point_name"], s["status"]) for s in board_route["stops"]] == [
        ("C", "IN_PROGRESS"),
        ("D", "PENDING"),
    ]
    assert "stop_total" not in board_route


async def test_done_route_of_today_stays_on_the_board(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _manager(db_session)
    service_type = await make_service_type(db_session)
    (point,) = await make_points(db_session, service_type=service_type, names=["Sala 1"])
    assert service_type.contractor_company is not None
    ana = await make_worker(db_session, company=service_type.contractor_company, full_name="Ana")
    route = await make_route(
        db_session, worker=ana, day=_TODAY, points=[point], status=RouteStatus.DONE
    )
    route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()

    body = _board(client)

    assert [(r["route_id"], r["status"], r["done_today"]) for r in body["routes"]] == [
        (str(route.id), "DONE", 1)
    ]
    assert body["not_started"] == []


async def test_route_less_alerts_of_today_reach_the_board(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _manager(db_session)

    def day_alert(key: str, status: AlertStatus, created_at: datetime) -> Alert:
        return Alert(
            kind=AlertKind.POINTS_UNSERVED,
            severity=AlertSeverity.WARNING,
            dedup_key=key,
            description="Pontos não atendidos",
            route_id=None,
            status=status,
            created_at=created_at,
        )

    # 21:30 on campus yesterday is already today in UTC: the campus day decides.
    late_yesterday = datetime(2026, 10, 20, 0, 30, tzinfo=UTC)
    db_session.add_all(
        [
            day_alert("open-today", AlertStatus.OPEN, _NOW - timedelta(hours=1)),
            day_alert("ack-today", AlertStatus.ACKNOWLEDGED, _NOW - timedelta(minutes=30)),
            day_alert("resolved-today", AlertStatus.RESOLVED, _NOW - timedelta(minutes=10)),
            day_alert("open-yesterday", AlertStatus.OPEN, late_yesterday),
        ]
    )
    await db_session.commit()

    body = _board(client)

    assert [(a["kind"], a["status"]) for a in body["open_alerts"]] == [
        ("POINTS_UNSERVED", "OPEN"),
        ("POINTS_UNSERVED", "ACKNOWLEDGED"),
    ]
    assert all(a["route_id"] is None for a in body["open_alerts"])
