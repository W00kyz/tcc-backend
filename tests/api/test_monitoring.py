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
from app.domain.audit.models import AuditTrail
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
from sqlalchemy import func, select
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


# --- alert lifecycle (spec §2 Ruling 7) --------------------------------------------------


async def _make_alert(
    db: AsyncSession,
    fx: _Fixture,
    route: Route,
    *,
    kind: AlertKind = AlertKind.LATE_STOP,
    status: AlertStatus = AlertStatus.OPEN,
) -> Alert:
    alert = Alert(
        kind=kind,
        severity=AlertSeverity.WARNING,
        dedup_key=f"{kind.value}:{route.id}:{status.value}",
        description="Ponto atrasado",
        route_id=route.id,
        route_stop_id=route.stops[0].id,
        field_worker_id=fx.worker.id,
        status=status,
    )
    db.add(alert)
    await db.flush()
    return alert


async def test_get_alerts_filters_by_status_and_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route_a = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    route_b = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    await _make_alert(db_session, fx, route_a, status=AlertStatus.OPEN)
    await _make_alert(db_session, fx, route_a, status=AlertStatus.RESOLVED)
    await _make_alert(db_session, fx, route_b, status=AlertStatus.OPEN)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    everything = client.get("/alerts", headers=_auth(token))
    assert {row["status"] for row in everything.json()} == {"OPEN", "RESOLVED"}
    assert len(everything.json()) == 3

    open_only = client.get("/alerts?status=OPEN", headers=_auth(token))
    assert [row["status"] for row in open_only.json()] == ["OPEN", "OPEN"]

    by_route = client.get(f"/alerts?route_id={route_a.id}", headers=_auth(token))
    assert {row["route_id"] for row in by_route.json()} == {str(route_a.id)}


async def test_acknowledge_sets_status_and_actor_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    alert = await _make_alert(db_session, fx, route)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.post(f"/alerts/{alert.id}/acknowledge", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ACKNOWLEDGED"
    assert response.json()["acknowledged_by"] == str(fx.manager.id)
    assert response.json()["acknowledged_at"] is not None

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "alert", AuditTrail.action == "acknowledge")
    )
    assert audit_count == 1


async def test_resolve_from_open_and_from_acknowledged(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    open_alert = await _make_alert(db_session, fx, route, status=AlertStatus.OPEN)
    ack_alert = await _make_alert(db_session, fx, route, status=AlertStatus.ACKNOWLEDGED)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    first = client.post(f"/alerts/{open_alert.id}/resolve", headers=_auth(token))
    assert first.json()["status"] == "RESOLVED"
    assert first.json()["resolved_by"] == str(fx.manager.id)

    second = client.post(f"/alerts/{ack_alert.id}/resolve", headers=_auth(token))
    assert second.json()["status"] == "RESOLVED"


async def test_acknowledge_after_resolve_is_conflict(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    route = await _make_route(db_session, fx, status=RouteStatus.IN_PROGRESS)
    alert = await _make_alert(db_session, fx, route, status=AlertStatus.RESOLVED)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.post(f"/alerts/{alert.id}/acknowledge", headers=_auth(token))

    assert response.status_code == 409


async def test_acknowledge_unknown_alert_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.post(
        "/alerts/00000000-0000-0000-0000-000000000000/acknowledge", headers=_auth(token)
    )

    assert response.status_code == 404


async def test_alert_endpoints_forbid_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.worker_user.email)

    assert client.get("/alerts", headers=_auth(token)).status_code == 403


# --- configurable alert rules (spec §2 Ruling 8) ---------------------------------------


async def test_get_alert_rules_returns_four_rows(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.get("/alert-rules", headers=_auth(token))

    assert response.status_code == 200, response.text
    kinds = {row["rule_kind"] for row in response.json()}
    assert kinds == {"LATE_START", "WORKER_ABSENT", "LATE_STOP", "NO_EVIDENCE"}
    assert all(row["enabled"] for row in response.json())


async def test_patch_alert_rule_persists_enabled_and_threshold(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    off = client.patch("/alert-rules/LATE_STOP", json={"enabled": False}, headers=_auth(token))
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    thr = client.patch(
        "/alert-rules/LATE_STOP", json={"threshold_minutes": 5}, headers=_auth(token)
    )
    assert thr.json()["threshold_minutes"] == 5
    # The earlier toggle is untouched by the partial patch.
    assert thr.json()["enabled"] is False

    rules = client.get("/alert-rules", headers=_auth(token)).json()
    late_stop = next(row for row in rules if row["rule_kind"] == "LATE_STOP")
    assert late_stop == {
        "rule_kind": "LATE_STOP",
        "enabled": False,
        "threshold_minutes": 5,
        "updated_at": late_stop["updated_at"],
    }
    assert late_stop["updated_at"] is not None


async def test_patch_unknown_rule_kind_is_404(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed_actors(db_session)
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.patch("/alert-rules/NOPE", json={"enabled": False}, headers=_auth(token))

    assert response.status_code == 404
