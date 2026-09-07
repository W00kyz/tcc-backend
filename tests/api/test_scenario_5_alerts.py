"""Cenário 5 ponta a ponta — verificação periódica, alerta de atraso e board em tempo real
(requisitos §7.5, design §2 Ruling 10).

The etapa deliverable: the periodic scan spots a stop that blew past its arrival window,
records ONE `LATE_STOP` alert with a severity and a description, fans a notification out to
the manager, and the alert shows up on the live board without a refresh; the manager then
acknowledges it and the very next board frame reflects that. Every hop runs against the real
Postgres/PostGIS testcontainer — `run_alert_scan` and `acknowledge` through a real HTTP
request, the SSE body driven directly.

What stays out of scope for pytest (manual author steps, same treatment as
`tests/api/test_checkin_timing.py` and `tests/api/test_scenario_4_reassignment.py`):
- the dashboard client and the "sem refresh" experience on a real browser — httpx's
  ASGITransport buffers the whole response and cannot drive an open-ended SSE stream, so the
  generator (`board_event_stream`) is exercised directly against the app's real session
  factory, exactly as `tests/api/test_monitoring_stream.py` does it;
- APScheduler's timer — the scan job is triggered explicitly here; the scheduler wiring is
  pinned in `tests/domain/monitoring/`.

Marked `slow` and excluded from the default run (`-m "not slow"` in pyproject `addopts`);
select it with `uv run pytest -m slow`.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.api import monitoring
from app.api.monitoring import board_event_stream
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.db.session import build_engine, build_session_factory
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity, AlertStatus, Notification
from app.domain.monitoring.scan import run_alert_scan
from app.domain.routing.models import Route, RouteStatus, RouteType
from app.domain.routing.service import StopInput, create_route
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"


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


@pytest_asyncio.fixture
async def session_factory(
    test_settings: Settings,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    # The stream generator opens a fresh short-lived session per snapshot from this factory.
    # It gets its OWN engine, bound to the test's event loop — the `client`'s engine lives on
    # the TestClient portal thread's loop, and asyncpg connections cannot cross loops. Both
    # point at the same testcontainer DB, so a commit made through an HTTP request is visible
    # to the next poll here.
    engine = build_engine(test_settings.database_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


class _Connection:
    """A controllable `is_disconnected` seam: stays connected until `drop()` is called."""

    def __init__(self) -> None:
        self._disconnected = False

    def drop(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


class _Seed:
    def __init__(
        self, *, manager: User, worker_user: User, worker: FieldWorker, route: Route
    ) -> None:
        self.manager = manager
        self.worker_user = worker_user
        self.worker = worker
        self.route = route


async def _seed(db: AsyncSession) -> _Seed:
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

    building = Building(name="Bloco CI", campus_area="CCT")
    db.add(building)
    await db.flush()
    floor = Floor(building_id=building.id, label="Terreo")
    db.add(floor)
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Ponto 0", description="Sala", latitude=-7.2, longitude=-35.9
    )
    db.add(point)
    await db.flush()

    route = await create_route(
        db,
        field_worker_id=worker.id,
        route_date=datetime.now(UTC).date(),
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=point.id)],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    route.status = RouteStatus.IN_PROGRESS
    # Well past the arrival window AND past the default 15 min `late_stop_tolerance_min`, so
    # the LATE_STOP rule fires on the first scan.
    route.stops[0].expected_arrival_to = datetime.now(UTC) - timedelta(hours=2)
    await db.flush()
    return _Seed(manager=manager, worker_user=worker_user, worker=worker, route=route)


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _next_frame(stream: AsyncGenerator[str], *, timeout: float = 5.0) -> str:
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


async def _next_board(stream: AsyncGenerator[str], *, timeout: float = 5.0) -> dict[str, Any]:
    """Read frames until a `event: board` one arrives (skipping any keep-alive), parse its JSON."""
    prefix = "event: board\ndata: "
    while True:
        frame = await _next_frame(stream, timeout=timeout)
        if frame.startswith(prefix):
            assert frame.endswith("\n\n"), repr(frame)
            parsed: dict[str, Any] = json.loads(frame[len(prefix) : -2])
            return parsed


@pytest.mark.slow
async def test_periodic_scan_alerts_and_realtime_board_end_to_end(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fast poll and a low keep-alive budget so the stream reacts within the test timeout
    # instead of on the production 2s / 15s cadence.
    monkeypatch.setattr(monitoring, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(monitoring, "_KEEPALIVE_SECONDS", 30.0)

    seed = await _seed(db_session)
    route_id = str(seed.route.id)
    stop_id = str(seed.route.stops[0].id)
    manager_id = str(seed.manager.id)
    await db_session.commit()

    # --- Step 1: the periodic check identifies the violation and registers ONE alert --------
    inserted = await run_alert_scan(db_session, now=datetime.now(UTC))
    assert len(inserted) == 1
    alert = inserted[0]
    alert_id = str(alert.id)
    assert alert.kind is AlertKind.LATE_STOP
    assert alert.severity is AlertSeverity.WARNING
    assert alert.status is AlertStatus.OPEN
    assert alert.description.strip() != ""
    assert str(alert.route_id) == route_id
    assert str(alert.route_stop_id) == stop_id

    alert_count = await db_session.scalar(select(func.count()).select_from(Alert))
    assert alert_count == 1

    # --- Step 2: a notification reached the manager (and only the manager) ------------------
    notifications = list(
        await db_session.scalars(select(Notification).where(Notification.kind == "ALERT"))
    )
    assert len(notifications) == 1
    assert str(notifications[0].user_id) == manager_id
    assert notifications[0].data["alert_id"] == alert_id

    # --- Step 3: the scan is idempotent — a second pass adds nothing -----------------------
    again = await run_alert_scan(db_session, now=datetime.now(UTC))
    assert again == []
    assert await db_session.scalar(select(func.count()).select_from(Alert)) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Notification).where(Notification.kind == "ALERT")
        )
        == 1
    )

    # --- Step 4: the alert shows on the live board WITHOUT a refresh -----------------------
    stream = board_event_stream(session_factory, _Connection().is_disconnected)
    try:
        board = await _next_board(stream)
        seeded = next(r for r in board["routes"] if r["route_id"] == route_id)
        assert seeded["is_delayed"] is True
        assert seeded["open_alert_count"] >= 1

        on_board = next(a for a in board["open_alerts"] if a["id"] == alert_id)
        assert on_board["kind"] == "LATE_STOP"
        assert on_board["status"] == "OPEN"

        # --- Step 5: the manager marks it handled -----------------------------------------
        token = _login(client, seed.manager.email)
        ack = client.post(f"/alerts/{alert_id}/acknowledge", headers=_auth(token))
        assert ack.status_code == 200, ack.text
        assert ack.json()["status"] == "ACKNOWLEDGED"

        # The next board frame reflects the acknowledgement: the OPEN count drops to 0 and the
        # alert now rides the board as ACKNOWLEDGED (`build_board` keeps OPEN+ACKNOWLEDGED in
        # `open_alerts` but only OPEN feeds `open_alert_count` / the delay signal).
        after_ack = await _next_board(stream)
        seeded_after = next(r for r in after_ack["routes"] if r["route_id"] == route_id)
        assert seeded_after["open_alert_count"] == 0
        acked = next(a for a in after_ack["open_alerts"] if a["id"] == alert_id)
        assert acked["status"] == "ACKNOWLEDGED"

        # --- Step 6: resolving it drops the alert off the board entirely ------------------
        resolved = client.post(f"/alerts/{alert_id}/resolve", headers=_auth(token))
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "RESOLVED"

        after_resolve = await _next_board(stream)
        assert all(a["id"] != alert_id for a in after_resolve["open_alerts"])
    finally:
        await stream.aclose()
