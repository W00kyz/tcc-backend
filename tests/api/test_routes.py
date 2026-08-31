from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
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
)
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
    app = create_app(settings=test_settings, mailer=recording_mailer, osrm_client=osrm)
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@contextmanager
def _client_with_osrm(
    test_settings: Settings, recording_mailer: RecordingMailer, osrm_client: FakeOsrmClient
) -> Iterator[TestClient]:
    # A one-off app whose OSRM seam differs from the module `osrm` fixture — needed by the
    # tests that require a specific trip order or a deliberate outage (spec §4.2, Ruling 6).
    app = create_app(settings=test_settings, mailer=recording_mailer, osrm_client=osrm_client)
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


async def test_patch_route_unknown_worker_404(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/routes", json=_create_route_payload(workers[0], [points[0]]), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/routes/{created['id']}",
        json={"field_worker_id": str(uuid4())},
        headers=_auth(token),
    )

    assert response.status_code == 404


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
        json={"field_worker_id": str(workers[1].id), "route_date": "2026-10-05"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["field_worker_id"] == str(workers[1].id)
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
