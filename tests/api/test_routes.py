from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    PointType,
    ServicePoint,
    ServiceType,
)
from app.domain.execution.models import Execution, ExecutionSource, ManualCompletion
from app.domain.forms.models import QuestionType
from app.domain.forms.service import add_question, get_or_create_form, publish_form
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    StopAssignment,
    StopAssignmentOutcome,
)
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"


@pytest.fixture
def osrm() -> FakeOsrmClient:
    return FakeOsrmClient()


@pytest.fixture
def client(
    test_settings: Settings, recording_mailer: RecordingMailer, osrm: FakeOsrmClient
) -> Iterator[TestClient]:
    # Overrides conftest.client locally so every route write in this file goes through the
    # in-memory fake instead of a real OSRM HTTP call (spec §8 — no test touches the network).
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=osrm,
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@contextmanager
def _client_with_osrm(
    test_settings: Settings, recording_mailer: RecordingMailer, osrm_client: FakeOsrmClient
) -> Iterator[TestClient]:
    # A one-off app whose OSRM seam differs from the module `osrm` fixture — needed by the
    # tests that require a specific trip order or a deliberate outage (spec §4.2, Ruling 6).
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=osrm_client,
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_manager_and_worker_route(
    db_session: AsyncSession,
) -> tuple[User, User, FieldWorker, Route]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="Sala", latitude=-7.2, longitude=-35.9
    )
    # full_name deliberately differs from worker_user.name: field_worker_name must come from
    # FieldWorker.full_name, not User.name — a regression test for that would silently pass if
    # the two strings matched.
    worker = FieldWorker(
        full_name="João da Silva", contractor_company_id=company.id, user_id=worker_user.id
    )
    db_session.add_all([point, worker])
    await db_session.flush()

    route = Route(field_worker_id=worker.id, route_date=date.today())
    db_session.add(route)
    await db_session.flush()
    db_session.add(RouteStop(route_id=route.id, service_point_id=point.id, order_index=1))
    await db_session.commit()
    return manager, worker_user, worker, route


async def _seed_actors_and_points(
    db_session: AsyncSession,
) -> tuple[User, User, list[FieldWorker], list[ServicePoint]]:
    """Manager + two field workers (the first one has an app login) + three service points on
    one floor. Returns them so the CRUD tests can build routes over real ids."""
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    points = [
        ServicePoint(
            floor_id=floor.id,
            name=f"Sala {index}",
            description="Sala",
            latitude=-7.2 + index * 0.001,
            longitude=-35.9 + index * 0.001,
            point_type=PointType.OCCASIONAL if index == 0 else PointType.REGULAR,
        )
        for index in range(3)
    ]
    workers = [
        FieldWorker(
            full_name="João da Silva", contractor_company_id=company.id, user_id=worker_user.id
        ),
        FieldWorker(full_name="Maria Souza", contractor_company_id=company.id),
    ]
    db_session.add_all([*points, *workers])
    await db_session.commit()
    return manager, worker_user, workers, points


def _create_route_payload(
    worker: FieldWorker, points: list[ServicePoint], route_date: str = "2026-09-01"
) -> dict[str, object]:
    return {
        "field_worker_id": str(worker.id),
        "route_date": route_date,
        "stops": [{"service_point_id": str(point.id)} for point in points],
    }


async def _add_route(
    db_session: AsyncSession,
    worker: FieldWorker,
    points: list[ServicePoint],
    *,
    route_date: date,
    status: RouteStatus = RouteStatus.PLANNED,
    with_geometry: bool = False,
) -> Route:
    """Insert a route with one stop per point directly, so the field-worker feed tests can pin
    a date, a status and a pre-drawn leg without going through the manager create endpoint."""
    route = Route(field_worker_id=worker.id, route_date=route_date, status=status)
    db_session.add(route)
    await db_session.flush()
    for index, point in enumerate(points, start=1):
        leg_geometry = (
            [[point.longitude, point.latitude], [point.longitude + 0.001, point.latitude]]
            if with_geometry and index > 1
            else None
        )
        db_session.add(
            RouteStop(
                route_id=route.id,
                service_point_id=point.id,
                order_index=index,
                leg_geometry=leg_geometry,
            )
        )
    await db_session.commit()
    return route


# --- existing endpoints (kept working through the new RouteOut shape) --------------------


async def test_field_worker_sees_only_their_own_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(route.id)
    assert len(body[0]["stops"]) == 1
    assert body[0]["field_worker_name"] == worker.full_name


# --- GET /routes/me (RF27 "rota do dia", enriched shared RouteOut) --------------------


async def test_routes_me_defaults_to_today_and_includes_geometry(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    today_route = await _add_route(
        db_session,
        workers[0],
        [points[0], points[1]],
        route_date=date.today(),
        with_geometry=True,
    )
    await _add_route(
        db_session, workers[0], [points[2]], route_date=date.today() - timedelta(days=1)
    )
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert [route["id"] for route in body] == [str(today_route.id)]  # yesterday's is excluded
    assert body[0]["route_type"] == "REGULAR"
    assert body[0]["stops"][0]["point_type"] == "OCCASIONAL"
    assert isinstance(body[0]["stops"][1]["leg_geometry"], list)


async def test_routes_me_embeds_the_active_form_per_stop(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    published_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    draft_only_type = ServiceType(name="Jardinagem", average_duration_minutes=45)
    db_session.add_all([published_type, draft_only_type])
    await db_session.flush()

    published_form = await get_or_create_form(db_session, service_type_id=published_type.id)
    await add_question(
        db_session,
        form_id=published_form.id,
        prompt="Piso lavado?",
        question_type=QuestionType.TEXT,
        required=True,
        options=[],
    )
    published_version = await publish_form(db_session, form_id=published_form.id)

    draft_form = await get_or_create_form(db_session, service_type_id=draft_only_type.id)
    await add_question(
        db_session,
        form_id=draft_form.id,
        prompt="Grama aparada?",
        question_type=QuestionType.TEXT,
        required=False,
        options=[],
    )

    route = Route(field_worker_id=workers[0].id, route_date=date.today())
    db_session.add(route)
    await db_session.flush()
    db_session.add_all(
        [
            RouteStop(
                route_id=route.id,
                service_point_id=points[0].id,
                order_index=1,
                service_type_id=published_type.id,
            ),
            RouteStop(route_id=route.id, service_point_id=points[1].id, order_index=2),
            RouteStop(
                route_id=route.id,
                service_point_id=points[2].id,
                order_index=3,
                service_type_id=draft_only_type.id,
            ),
        ]
    )
    await db_session.commit()
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    stops = response.json()[0]["stops"]

    assert stops[0]["service_type_name"] == "Limpeza"
    assert stops[0]["service_type_id"] == str(published_type.id)
    assert stops[0]["form"]["form_version_id"] == str(published_version.id)
    assert len(stops[0]["form"]["questions"][0]["content_hash"]) == 64

    assert stops[1]["service_type_id"] is None
    assert stops[1]["service_type_name"] is None
    assert stops[1]["form"] is None

    assert stops[2]["service_type_name"] == "Jardinagem"
    assert stops[2]["form"] is None  # only a draft version exists


async def test_routes_me_filters_by_date(client: TestClient, db_session: AsyncSession) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    tomorrow = date.today() + timedelta(days=1)
    await _add_route(db_session, workers[0], [points[0]], route_date=date.today())
    await _add_route(db_session, workers[0], [points[1]], route_date=tomorrow)
    token = _login(client, worker_user.email)

    response = client.get(f"/routes/me?date={tomorrow.isoformat()}", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["route_date"] == tomorrow.isoformat()


async def test_routes_me_hides_cancelled(client: TestClient, db_session: AsyncSession) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    await _add_route(
        db_session,
        workers[0],
        [points[0]],
        route_date=date.today(),
        status=RouteStatus.CANCELLED,
    )
    await _add_route(db_session, workers[0], [points[1]], route_date=date.today())
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "PLANNED"


async def test_start_sets_status_in_progress(client: TestClient, db_session: AsyncSession) -> None:
    _manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")
    body = {"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()}

    start = client.post(f"/routes/{route.id}/start", json=body, headers=_auth(token))

    assert start.status_code == 200
    assert start.json()["status"] == "IN_PROGRESS"
    feed = client.get("/routes/me", headers=_auth(token))
    assert feed.json()[0]["status"] == "IN_PROGRESS"


async def test_start_cancelled_route_409(client: TestClient, db_session: AsyncSession) -> None:
    _manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    db_route = await db_session.get(Route, route.id)
    assert db_route is not None
    db_route.status = RouteStatus.CANCELLED
    await db_session.commit()
    token = _login(client, "joao@empresa.com")

    response = client.post(
        f"/routes/{route.id}/start",
        json={"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()},
        headers=_auth(token),
    )

    assert response.status_code == 409
    # Pin it to the RouteNotStartable path, not the started_at guard (which never ran here).
    assert "CANCELLED" in response.json()["detail"]
    assert "PLANNED" in response.json()["detail"]


async def test_worker_can_start_their_own_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")

    response = client.post(
        f"/routes/{route.id}/start",
        json={"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["started_at"] is not None
    assert body["field_worker_name"] == worker.full_name


async def test_starting_an_already_started_route_returns_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")
    body = {"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()}
    client.post(f"/routes/{route.id}/start", json=body, headers=_auth(token))

    response = client.post(f"/routes/{route.id}/start", json=body, headers=_auth(token))

    assert response.status_code == 409


async def test_start_route_records_audit_row(client: TestClient, db_session: AsyncSession) -> None:
    _manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, "joao@empresa.com")
    body = {"latitude": -7.2, "longitude": -35.9, "started_at": datetime.now(UTC).isoformat()}

    assert (
        client.post(f"/routes/{route.id}/start", json=body, headers=_auth(token)).status_code == 200
    )

    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "start")
    )
    assert count == 1


# --- POST /routes ----------------------------------------------------------------------


async def test_manager_creates_route_with_ordered_stops(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PLANNED"
    assert body["field_worker_id"] == str(workers[0].id)
    assert [stop["service_point_id"] for stop in body["stops"]] == [
        str(points[0].id),
        str(points[1].id),
    ]
    assert body["stops"][0]["order_index"] == 1
    assert body["stops"][0]["leg_geometry"] is None
    assert body["stops"][1]["leg_geometry"] is not None
    assert body["stops"][0]["building_name"] == "Bloco CI"
    assert body["stops"][0]["floor_label"] == "Térreo"
    assert "routing_degraded" in body
    assert body["routing_degraded"] is False


async def test_create_route_stop_carries_service_type(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    service_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    db_session.add(service_type)
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.post(
        "/routes",
        json={
            "field_worker_id": str(workers[0].id),
            "route_date": "2026-09-01",
            "stops": [
                {"service_point_id": str(points[0].id), "service_type_id": str(service_type.id)}
            ],
        },
        headers=_auth(token),
    )

    assert response.status_code == 201
    created = response.json()
    assert created["stops"][0]["service_type_id"] == str(service_type.id)
    assert created["stops"][0]["service_type_name"] == "Limpeza"

    fetched = client.get(f"/routes/{created['id']}", headers=_auth(token))
    assert fetched.json()["stops"][0]["service_type_id"] == str(service_type.id)


async def test_create_route_writes_audit_trail(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    )
    assert response.status_code == 201

    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "create")
    )
    assert count == 1


async def test_field_worker_cannot_create_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, worker_user.email)

    response = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    )

    assert response.status_code == 403


async def test_create_route_unknown_service_point_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes",
        json={
            "field_worker_id": str(workers[0].id),
            "route_date": "2026-09-01",
            "stops": [{"service_point_id": str(uuid4())}],
        },
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_create_route_unknown_worker_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes",
        json={
            "field_worker_id": str(uuid4()),
            "route_date": "2026-09-01",
            "stops": [{"service_point_id": str(points[0].id)}],
        },
        headers=_auth(token),
    )

    assert response.status_code == 404


# --- POST /routes/occasional (Ruling 2) ----------------------------------------------


async def test_occasional_creates_one_route_per_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes/occasional",
        json={
            "field_worker_ids": [str(workers[0].id), str(workers[1].id)],
            "route_date": "2026-09-01",
            "stops": [
                {"service_point_id": str(points[0].id)},
                {"service_point_id": str(points[1].id)},
            ],
        },
        headers=_auth(token),
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body) == 2
    assert {route["field_worker_id"] for route in body} == {str(workers[0].id), str(workers[1].id)}
    assert all(route["route_type"] == "OCCASIONAL" for route in body)
    assert [stop["service_point_id"] for stop in body[0]["stops"]] == [
        stop["service_point_id"] for stop in body[1]["stops"]
    ]
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "create")
    )
    assert count == 2


async def test_occasional_unknown_worker_404_commits_nothing(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/routes/occasional",
        json={
            "field_worker_ids": [str(workers[0].id), str(uuid4())],
            "route_date": "2026-09-01",
            "stops": [{"service_point_id": str(points[0].id)}],
        },
        headers=_auth(token),
    )

    assert response.status_code == 404
    count = await db_session.scalar(select(func.count()).select_from(Route))
    assert count == 0


async def test_occasional_forbidden_for_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, worker_user.email)

    response = client.post(
        "/routes/occasional",
        json={
            "field_worker_ids": [str(workers[0].id)],
            "route_date": "2026-09-01",
            "stops": [{"service_point_id": str(points[0].id)}],
        },
        headers=_auth(token),
    )

    assert response.status_code == 403


# --- GET /routes (filters) ------------------------------------------------------------


async def test_list_routes_filters_by_date_and_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    first = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0]], route_date="2026-09-01"),
        headers=_auth(token),
    ).json()
    second = client.post(
        "/routes",
        json=_create_route_payload(workers[1], [points[1]], route_date="2026-09-02"),
        headers=_auth(token),
    ).json()

    by_date = client.get("/routes?date=2026-09-01", headers=_auth(token)).json()
    assert [route["id"] for route in by_date] == [first["id"]]

    by_worker = client.get(f"/routes?field_worker_id={workers[1].id}", headers=_auth(token)).json()
    assert [route["id"] for route in by_worker] == [second["id"]]

    by_type = client.get("/routes?route_type=OCCASIONAL", headers=_auth(token)).json()
    assert by_type == []  # OCCASIONAL is valid, just unmatched — an empty list, not a 422.

    by_status = client.get("/routes?status=PLANNED", headers=_auth(token)).json()
    assert {route["id"] for route in by_status} == {first["id"], second["id"]}


async def test_get_route_by_id_404_for_unknown(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.get(f"/routes/{uuid4()}", headers=_auth(token))

    assert response.status_code == 404


async def test_get_route_by_id_returns_full_shape(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    ).json()

    response = client.get(f"/routes/{created['id']}", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert len(response.json()["stops"]) == 2


# --- PATCH /routes/{id} --------------------------------------------------------------


async def test_patch_route_reorders_stops(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    ).json()

    response = client.patch(
        f"/routes/{created['id']}",
        json={
            "stops": [
                {"service_point_id": str(points[1].id)},
                {"service_point_id": str(points[0].id)},
            ]
        },
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert [stop["service_point_id"] for stop in body["stops"]] == [
        str(points[1].id),
        str(points[0].id),
    ]
    assert body["stops"][1]["leg_geometry"][0] == [points[1].longitude, points[1].latitude]


async def test_patch_route_updates_service_type_on_existing_pending_stop(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for the `pending_by_point` update-in-place branch of
    `replace_route_stops`: a PATCH that keeps an already-PENDING stop's `service_point_id`
    but changes its `service_type_id` must persist that change, not just reorder it."""
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    first_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    second_type = ServiceType(name="Jardinagem", average_duration_minutes=45)
    db_session.add_all([first_type, second_type])
    await db_session.commit()
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json={
            "field_worker_id": str(workers[0].id),
            "route_date": "2026-09-01",
            "stops": [
                {"service_point_id": str(points[0].id), "service_type_id": str(first_type.id)}
            ],
        },
        headers=_auth(token),
    ).json()
    assert created["stops"][0]["service_type_id"] == str(first_type.id)

    response = client.patch(
        f"/routes/{created['id']}",
        json={
            "stops": [
                {"service_point_id": str(points[0].id), "service_type_id": str(second_type.id)}
            ]
        },
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stops"][0]["service_point_id"] == str(points[0].id)
    assert body["stops"][0]["service_type_id"] == str(second_type.id)
    assert body["stops"][0]["service_type_name"] == "Jardinagem"


async def test_patch_route_rejects_removing_done_stop(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    ).json()
    first_stop = await db_session.scalar(
        select(RouteStop).where(RouteStop.route_id == created["id"]).order_by(RouteStop.order_index)
    )
    assert first_stop is not None
    first_stop.status = RouteStopStatus.DONE
    await db_session.commit()

    response = client.patch(
        f"/routes/{created['id']}",
        json={"stops": [{"service_point_id": str(points[1].id)}]},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_patch_cancelled_route_409(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()
    route = await db_session.get(Route, created["id"])
    assert route is not None
    route.status = RouteStatus.CANCELLED
    await db_session.commit()

    response = client.patch(
        f"/routes/{created['id']}",
        json={"stops": [{"service_point_id": str(points[1].id)}]},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_patch_route_rejects_changing_the_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    """PATCH must not be a second reassignment path: changing field_worker_id here bypasses the
    append-only stop_assignments chain that POST /routes/{id}/reassign maintains (spec §3.4
    Ruling 1 / RF21). A differing worker is rejected outright — no partial write."""
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    worker_a_id, worker_b_id = workers[0].id, workers[1].id
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()
    assignments_before = await db_session.scalar(select(func.count()).select_from(StopAssignment))

    response = client.patch(
        f"/routes/{created['id']}",
        json={"field_worker_id": str(worker_b_id)},
        headers=_auth(token),
    )

    assert response.status_code == 409
    assert "reassign" in response.json()["detail"]
    current_worker_id = await db_session.scalar(
        select(Route.field_worker_id).where(Route.id == created["id"])
    )
    assert current_worker_id == worker_a_id
    assignments_after = await db_session.scalar(select(func.count()).select_from(StopAssignment))
    assert assignments_after == assignments_before
    update_audits = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "update")
    )
    assert update_audits == 0


async def test_patch_route_accepts_the_unchanged_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Echoing the current field_worker_id back (as the dashboard edit form does) is a no-op for
    the worker — it is not validated and not rewritten — and the rest of the PATCH still applies."""
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/routes/{created['id']}",
        json={"field_worker_id": str(workers[0].id), "route_date": "2026-10-05"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["field_worker_id"] == str(workers[0].id)
    assert body["route_date"] == "2026-10-05"


async def test_patch_route_updates_scalar_fields_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/routes/{created['id']}",
        json={"route_date": "2026-10-05", "scheduled_start_at": "2026-10-05T08:00:00Z"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["field_worker_id"] == str(workers[0].id)
    assert body["route_date"] == "2026-10-05"
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "update")
    )
    assert count == 1


# --- GET /routes (Task-4 review: invalid filter -> 422, no-filter, RBAC) --------------


async def test_manager_with_no_filters_sees_all_routes(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    first = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0]], route_date="2026-09-01"),
        headers=_auth(token),
    ).json()
    second = client.post(
        "/routes",
        json=_create_route_payload(workers[1], [points[1]], route_date="2026-09-02"),
        headers=_auth(token),
    ).json()

    body = client.get("/routes", headers=_auth(token)).json()

    assert {route["id"] for route in body} == {first["id"], second["id"]}


async def test_list_routes_invalid_route_type_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.get("/routes?route_type=BOGUS", headers=_auth(token))

    assert response.status_code == 422
    assert "BOGUS" in response.json()["detail"]


async def test_list_routes_invalid_status_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.get("/routes?status=BOGUS", headers=_auth(token))

    assert response.status_code == 422
    assert "BOGUS" in response.json()["detail"]


async def test_field_worker_cannot_read_or_patch_routes(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, worker_user.email)
    some_id = uuid4()

    assert client.get("/routes", headers=_auth(token)).status_code == 403
    assert client.get(f"/routes/{some_id}", headers=_auth(token)).status_code == 403
    assert (
        client.patch(
            f"/routes/{some_id}", json={"route_date": "2026-10-05"}, headers=_auth(token)
        ).status_code
        == 403
    )


# --- POST /routes/{id}/optimize, /reassign, /cancel ---------------------------------


async def test_optimize_applies_osrm_order(
    test_settings: Settings, recording_mailer: RecordingMailer, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    with _client_with_osrm(
        test_settings, recording_mailer, FakeOsrmClient(order=[0, 2, 1])
    ) as client:
        token = _login(client, manager.email)
        created = client.post(
            "/routes",
            json=_create_route_payload(workers[0], [points[0], points[1], points[2]]),
            headers=_auth(token),
        ).json()

        response = client.post(f"/routes/{created['id']}/optimize", headers=_auth(token))

    assert response.status_code == 200
    assert [stop["service_point_id"] for stop in response.json()["stops"]] == [
        str(points[0].id),
        str(points[2].id),
        str(points[1].id),
    ]


async def test_optimize_returns_503_when_osrm_down(
    client: TestClient,
    test_settings: Settings,
    recording_mailer: RecordingMailer,
    db_session: AsyncSession,
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    ).json()

    with _client_with_osrm(
        test_settings, recording_mailer, FakeOsrmClient(unavailable=True)
    ) as down_client:
        down_token = _login(down_client, manager.email)
        response = down_client.post(f"/routes/{created['id']}/optimize", headers=_auth(down_token))

    assert response.status_code == 503
    assert "OSRM" in response.json()["detail"]


async def test_optimize_cancelled_route_409(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes",
        json=_create_route_payload(workers[0], [points[0], points[1]]),
        headers=_auth(token),
    ).json()
    route = await db_session.get(Route, created["id"])
    assert route is not None
    route.status = RouteStatus.CANCELLED
    await db_session.commit()

    response = client.post(f"/routes/{created['id']}/optimize", headers=_auth(token))

    assert response.status_code == 409


async def test_reassign_moves_route_and_appends_chain(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.post(
        f"/routes/{created['id']}/reassign",
        json={"field_worker_id": str(workers[1].id), "reason": "coverage swap"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json()["field_worker_id"] == str(workers[1].id)
    seq2 = (
        await db_session.scalars(select(StopAssignment).where(StopAssignment.sequence == 2))
    ).all()
    assert len(seq2) == 1
    assert all(assignment.field_worker_id == workers[1].id for assignment in seq2)
    seq1 = (
        await db_session.scalars(select(StopAssignment).where(StopAssignment.sequence == 1))
    ).all()
    assert len(seq1) == 1
    assert all(assignment.outcome == StopAssignmentOutcome.REASSIGNED for assignment in seq1)
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route", AuditTrail.action == "reassign")
    )
    assert count == 1


async def test_reassign_unknown_worker_404(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.post(
        f"/routes/{created['id']}/reassign",
        json={"field_worker_id": str(uuid4()), "reason": "coverage swap"},
        headers=_auth(token),
    )

    assert response.status_code == 404


async def test_reassign_cancelled_route_409(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()
    route = await db_session.get(Route, created["id"])
    assert route is not None
    route.status = RouteStatus.CANCELLED
    await db_session.commit()

    response = client.post(
        f"/routes/{created['id']}/reassign",
        json={"field_worker_id": str(workers[1].id), "reason": "coverage swap"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_route_action_unknown_route_404_for_manager(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.post(
        f"/routes/{uuid4()}/cancel", json={"reason": "gone"}, headers=_auth(token)
    )

    assert response.status_code == 404


async def test_reassign_requires_reason(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.post(
        f"/routes/{created['id']}/reassign",
        json={"field_worker_id": str(workers[1].id), "reason": ""},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_cancel_sets_status(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    first = client.post(
        f"/routes/{created['id']}/cancel",
        json={"reason": "building closed"},
        headers=_auth(token),
    )
    second = client.post(
        f"/routes/{created['id']}/cancel",
        json={"reason": "building closed"},
        headers=_auth(token),
    )

    assert first.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert second.status_code == 409


async def test_route_actions_forbidden_for_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, worker_user.email)
    some_id = uuid4()

    assert client.post(f"/routes/{some_id}/optimize", headers=_auth(token)).status_code == 403
    assert (
        client.post(
            f"/routes/{some_id}/reassign",
            json={"field_worker_id": str(workers[1].id), "reason": "x"},
            headers=_auth(token),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/routes/{some_id}/cancel", json={"reason": "x"}, headers=_auth(token)
        ).status_code
        == 403
    )


# --- POST /routes/{id}/stops/{stop_id}/complete-manually (RF53) ----------------------


async def _start_route(db_session: AsyncSession, route: Route) -> None:
    db_route = await db_session.get(Route, route.id)
    assert db_route is not None
    db_route.status = RouteStatus.IN_PROGRESS
    db_route.started_at = datetime.now(UTC)
    await db_session.commit()


async def _first_stop_id(db_session: AsyncSession, route: Route) -> str:
    stop = await db_session.scalar(
        select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.order_index)
    )
    assert stop is not None
    return str(stop.id)


async def test_complete_manually_marks_done_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    manager_id = manager.id
    await _start_route(db_session, route)
    stop_id = await _first_stop_id(db_session, route)
    token = _login(client, manager.email)

    response = client.post(
        f"/routes/{route.id}/stops/{stop_id}/complete-manually",
        json={"reason": "celular quebrado"},
        headers=_auth(token),
    )

    assert response.status_code == 201
    body = response.json()
    done = [stop for stop in body["stops"] if stop["id"] == stop_id]
    assert len(done) == 1
    assert done[0]["status"] == "DONE"

    manual = await db_session.scalar(
        select(ManualCompletion).where(ManualCompletion.route_stop_id == stop_id)
    )
    assert manual is not None
    assert manual.reason == "celular quebrado"
    assert manual.completed_by == manager_id
    assert manual.completed_at is not None

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route_stop", AuditTrail.action == "complete_manually")
    )
    assert audit_count == 1

    execution = await db_session.scalar(select(Execution).where(Execution.route_stop_id == stop_id))
    assert execution is not None
    assert execution.source == ExecutionSource.MANAGER_MANUAL


async def test_complete_manually_empty_reason_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    await _start_route(db_session, route)
    stop_id = await _first_stop_id(db_session, route)
    token = _login(client, manager.email)

    response = client.post(
        f"/routes/{route.id}/stops/{stop_id}/complete-manually",
        json={"reason": ""},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_complete_manually_already_done_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    await _start_route(db_session, route)
    stop_id = await _first_stop_id(db_session, route)
    token = _login(client, manager.email)
    url = f"/routes/{route.id}/stops/{stop_id}/complete-manually"

    first = client.post(url, json={"reason": "celular quebrado"}, headers=_auth(token))
    second = client.post(url, json={"reason": "de novo"}, headers=_auth(token))

    assert first.status_code == 201
    assert second.status_code == 409


async def test_complete_manually_cancelled_route_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    stop_id = await _first_stop_id(db_session, route)
    db_route = await db_session.get(Route, route.id)
    assert db_route is not None
    db_route.status = RouteStatus.CANCELLED
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.post(
        f"/routes/{route.id}/stops/{stop_id}/complete-manually",
        json={"reason": "prédio fechado"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_complete_manually_field_worker_forbidden_403(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, _worker, route = await _seed_manager_and_worker_route(db_session)
    token = _login(client, worker_user.email)

    response = client.post(
        f"/routes/{route.id}/stops/{uuid4()}/complete-manually",
        json={"reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 403


async def test_complete_manually_stop_not_on_route_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, worker, route = await _seed_manager_and_worker_route(db_session)
    point_id = await db_session.scalar(select(ServicePoint.id))
    other_route = Route(field_worker_id=worker.id, route_date=date.today())
    db_session.add(other_route)
    await db_session.flush()
    other_stop = RouteStop(route_id=other_route.id, service_point_id=point_id, order_index=1)
    db_session.add(other_stop)
    await db_session.commit()
    other_stop_id = str(other_stop.id)
    token = _login(client, manager.email)

    response = client.post(
        f"/routes/{route.id}/stops/{other_stop_id}/complete-manually",
        json={"reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 404
