"""Etapa 9 — the `GET /monitoring/stream` SSE surface (spec §2 Ruling 4).

The streaming behaviour is exercised against `board_event_stream` directly, using the app's
real testcontainer session factory: httpx's ASGITransport buffers the whole response and
cannot drive an open-ended SSE stream, so a live server would be the only alternative. Every
frame read is wrapped in `asyncio.wait_for` so a stuck generator fails the test instead of
hanging. RBAC is checked through the mounted route, where the 403 is raised before the stream
starts."""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.api import monitoring
from app.api.monitoring import board_event_stream
from app.core.campus_clock import campus_today
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
from app.domain.routing.models import Route, RouteStatus, RouteType
from app.domain.routing.service import StopInput, create_route
from app.main import create_app
from fastapi.testclient import TestClient
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


@pytest.fixture
def session_factory(client: TestClient) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = client.app.state.session_factory  # type: ignore[attr-defined]
    return factory


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep these tests fast: a 50ms poll instead of the production 2s. The keep-alive interval
    # is left at its default except where a test overrides it.
    monkeypatch.setattr(monitoring, "_POLL_SECONDS", 0.05)


class _Seed:
    def __init__(self, *, manager: User, worker_user: User, worker: FieldWorker) -> None:
        self.manager = manager
        self.worker_user = worker_user
        self.worker = worker


async def _seed_actors(db: AsyncSession) -> _Seed:
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
    return _Seed(manager=manager, worker_user=worker_user, worker=worker)


async def _make_route(db: AsyncSession, seed: _Seed) -> Route:
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
        field_worker_id=seed.worker.id,
        # The board's "today" is the campus calendar day: from 21:00 in Campina Grande the UTC
        # date is already tomorrow, and a UTC-dated route fell off the board (test failed at night).
        route_date=campus_today(datetime.now(UTC)),
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=point.id)],
        actor_id=seed.manager.id,
        osrm=FakeOsrmClient(),
    )
    route.status = RouteStatus.IN_PROGRESS
    await db.flush()
    return route


class _Connection:
    """A controllable `is_disconnected` seam: stays connected until `drop()` is called."""

    def __init__(self) -> None:
        self._disconnected = False

    def drop(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


async def _next_frame(stream: AsyncGenerator[str], *, timeout: float = 5.0) -> str:
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


def _board_payload(frame: str) -> dict[str, object]:
    prefix = "event: board\ndata: "
    assert frame.startswith(prefix), repr(frame)
    assert frame.endswith("\n\n"), repr(frame)
    parsed: dict[str, object] = json.loads(frame[len(prefix) : -2])
    return parsed


async def test_first_frame_is_the_board_snapshot(
    session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    await _seed_actors(db_session)
    await db_session.commit()

    stream = board_event_stream(session_factory, _Connection().is_disconnected)
    frame = await _next_frame(stream)
    await stream.aclose()

    payload = _board_payload(frame)
    assert payload["routes"] == []
    assert payload["open_alerts"] == []
    assert payload["worker_positions"] == []
    assert payload["generated_at"] is not None


async def test_stream_re_emits_when_an_alert_appears(
    session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    seed = await _seed_actors(db_session)
    route = await _make_route(db_session, seed)
    await db_session.commit()

    stream = board_event_stream(session_factory, _Connection().is_disconnected)
    first = _board_payload(await _next_frame(stream))
    assert first["open_alerts"] == []

    db_session.add(
        Alert(
            kind=AlertKind.LATE_STOP,
            severity=AlertSeverity.WARNING,
            dedup_key=f"LATE_STOP:{route.stops[0].id}",
            description="Ponto atrasado",
            route_id=route.id,
            route_stop_id=route.stops[0].id,
            field_worker_id=seed.worker.id,
            status=AlertStatus.OPEN,
        )
    )
    await db_session.commit()

    second = _board_payload(await _next_frame(stream, timeout=8))
    await stream.aclose()

    open_alerts = second["open_alerts"]
    assert isinstance(open_alerts, list)
    assert len(open_alerts) == 1
    assert open_alerts[0]["kind"] == "LATE_STOP"
    routes = second["routes"]
    assert isinstance(routes, list)
    assert routes[0]["is_delayed"] is True


async def test_idle_stream_emits_keep_alive_not_a_fresh_board(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitoring, "_KEEPALIVE_SECONDS", 0.0)
    seed = await _seed_actors(db_session)
    route = await _make_route(db_session, seed)
    # A stale GPS fix: its `age_seconds` advances on every poll. If it were part of the change
    # digest the stream would emit a fresh board every tick instead of a keep-alive.
    db_session.add(
        WorkerPosition(
            field_worker_id=seed.worker.id,
            route_id=route.id,
            latitude=-7.1,
            longitude=-35.8,
            reported_at=datetime.now(UTC) - timedelta(seconds=90),
        )
    )
    await db_session.commit()

    stream = board_event_stream(session_factory, _Connection().is_disconnected)
    assert (await _next_frame(stream)).startswith("event: board")
    second = await _next_frame(stream)
    await stream.aclose()

    assert second == ": keep-alive\n\n"


async def test_stream_stops_when_the_client_disconnects(
    session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    await _seed_actors(db_session)
    await db_session.commit()

    connection = _Connection()
    stream = board_event_stream(session_factory, connection.is_disconnected)
    assert (await _next_frame(stream)).startswith("event: board")

    connection.drop()
    with pytest.raises(StopAsyncIteration):
        await _next_frame(stream)


async def test_field_worker_forbidden(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed_actors(db_session)
    await db_session.commit()
    token = client.post(
        "/auth/login", json={"email": seed.worker_user.email, "password": _PASSWORD}
    ).json()["access_token"]

    response = client.get("/monitoring/stream", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


async def test_manager_gets_an_event_stream_response(
    client: TestClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route-level smoke test: the mounted endpoint runs the manager through auth, releases its
    auth DB session (F-1) and answers 200 with the SSE content type. The stream body is swapped
    for a finite generator here — httpx's ASGITransport buffers the response and cannot drain an
    open-ended one."""

    async def _finite_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str]:
        yield ": ready\n\n"

    monkeypatch.setattr(monitoring, "board_event_stream", _finite_stream)

    seed = await _seed_actors(db_session)
    await db_session.commit()
    token = client.post(
        "/auth/login", json={"email": seed.manager.email, "password": _PASSWORD}
    ).json()["access_token"]

    response = client.get("/monitoring/stream", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
