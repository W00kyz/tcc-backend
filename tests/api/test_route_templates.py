"""`/route-templates` CRUD + `/materialize` (RF15, spec §3.3 / §5.2 Ruling 8) and
`POST /routes/occasional` (Ruling 2). Every route write goes through the in-memory OSRM fake
(spec §8 — no test touches the network)."""

from collections.abc import Iterator
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
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import Route
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
    app = create_app(settings=test_settings, mailer=recording_mailer, osrm_client=osrm)
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db_session: AsyncSession,
) -> tuple[User, User, list[FieldWorker], list[ServicePoint]]:
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


def _template_payload(
    points: list[ServicePoint], *, worker: FieldWorker | None = None
) -> dict[str, object]:
    return {
        "name": "Limpeza diária — Bloco CI",
        "field_worker_id": str(worker.id) if worker is not None else None,
        "recurrence": "DAILY",
        "route_type": "REGULAR",
        "stops": [
            {"service_point_id": str(points[1].id), "expected_arrival_from": "08:00:00"},
            {"service_point_id": str(points[0].id)},
        ],
    }


# --- POST/GET /route-templates ------------------------------------------------------------


async def test_manager_creates_template_with_ordered_stops(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/route-templates", json=_template_payload(points, worker=workers[0]), headers=_auth(token)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Limpeza diária — Bloco CI"
    assert body["is_active"] is True
    assert body["recurrence"] == "DAILY"
    assert [stop["service_point_id"] for stop in body["stops"]] == [
        str(points[1].id),
        str(points[0].id),
    ]
    assert body["stops"][0]["order_index"] == 1
    assert body["stops"][0]["expected_arrival_from"] == "08:00:00"


async def test_list_templates_returns_all(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    client.post("/route-templates", json=_template_payload(points), headers=_auth(token))

    response = client.get("/route-templates", headers=_auth(token))

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert len(response.json()[0]["stops"]) == 2


async def test_create_template_writes_audit_trail(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)

    client.post("/route-templates", json=_template_payload(points), headers=_auth(token))

    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route_template", AuditTrail.action == "create")
    )
    assert count == 1


async def test_weekly_template_without_weekdays_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    payload = _template_payload(points)
    payload["recurrence"] = "WEEKLY"

    response = client.post("/route-templates", json=payload, headers=_auth(token))

    assert response.status_code == 422


@pytest.mark.parametrize("weekdays", [[0], [8], []])
async def test_weekly_template_with_out_of_range_weekdays_is_422(
    weekdays: list[int], client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    payload = _template_payload(points)
    payload["recurrence"] = "WEEKLY"
    payload["weekdays"] = weekdays

    response = client.post("/route-templates", json=payload, headers=_auth(token))

    assert response.status_code == 422


async def test_weekly_template_with_valid_weekdays_is_created(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    payload = _template_payload(points)
    payload["recurrence"] = "WEEKLY"
    payload["weekdays"] = [1, 3, 5]

    response = client.post("/route-templates", json=payload, headers=_auth(token))

    assert response.status_code == 201
    assert response.json()["weekdays"] == [1, 3, 5]


async def test_field_worker_cannot_touch_templates(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, _workers, points = await _seed(db_session)
    token = _login(client, worker_user.email)

    assert client.get("/route-templates", headers=_auth(token)).status_code == 403
    assert (
        client.post(
            "/route-templates", json=_template_payload(points), headers=_auth(token)
        ).status_code
        == 403
    )


# --- PATCH /route-templates/{id} --------------------------------------------------------


async def test_patch_template_name_and_deactivates(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/route-templates/{created['id']}",
        json={"name": "Novo nome", "is_active": False},
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Novo nome"
    assert response.json()["is_active"] is False
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "route_template", AuditTrail.action == "update")
    )
    assert count == 1


async def test_patch_template_replaces_stop_list(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/route-templates/{created['id']}",
        json={"stops": [{"service_point_id": str(points[2].id)}]},
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert [stop["service_point_id"] for stop in response.json()["stops"]] == [str(points[2].id)]


async def test_patch_template_to_weekly_without_weekdays_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/route-templates/{created['id']}",
        json={"recurrence": "WEEKLY"},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_patch_route_template_unknown_worker_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points), headers=_auth(token)
    ).json()

    response = client.patch(
        f"/route-templates/{created['id']}",
        json={"field_worker_id": str(uuid4())},
        headers=_auth(token),
    )

    assert response.status_code == 404


async def test_patch_unknown_template_is_404(client: TestClient, db_session: AsyncSession) -> None:
    manager, _worker_user, _workers, _points = await _seed(db_session)
    token = _login(client, manager.email)

    response = client.patch(f"/route-templates/{uuid4()}", json={"name": "x"}, headers=_auth(token))

    assert response.status_code == 404


# --- POST /route-templates/{id}/materialize -------------------------------------------


async def test_materialize_builds_a_route_from_the_template(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points, worker=workers[0]), headers=_auth(token)
    ).json()

    response = client.post(
        f"/route-templates/{created['id']}/materialize",
        json={"route_date": "2026-09-07"},
        headers=_auth(token),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["route_type"] == "REGULAR"
    assert body["field_worker_id"] == str(workers[0].id)
    assert [stop["service_point_id"] for stop in body["stops"]] == [
        str(points[1].id),
        str(points[0].id),
    ]
    assert body["stops"][0]["expected_arrival_from"] == "2026-09-07T08:00:00Z"
    assert body["stops"][1]["expected_arrival_from"] is None

    route = await db_session.get(Route, body["id"])
    assert route is not None
    assert route.template_id is not None
    assert str(route.template_id) == created["id"]


async def test_materialize_occasional_template_sets_route_type(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    payload = _template_payload(points, worker=workers[0])
    payload["route_type"] = "OCCASIONAL"
    created = client.post("/route-templates", json=payload, headers=_auth(token)).json()
    assert created["route_type"] == "OCCASIONAL"

    response = client.post(
        f"/route-templates/{created['id']}/materialize",
        json={"route_date": "2026-09-07"},
        headers=_auth(token),
    )

    assert response.status_code == 201
    assert response.json()["route_type"] == "OCCASIONAL"


async def test_materialize_body_worker_overrides_template(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points, worker=workers[0]), headers=_auth(token)
    ).json()

    response = client.post(
        f"/route-templates/{created['id']}/materialize",
        json={"route_date": "2026-09-07", "field_worker_id": str(workers[1].id)},
        headers=_auth(token),
    )

    assert response.status_code == 201
    assert response.json()["field_worker_id"] == str(workers[1].id)


async def test_materialize_without_any_worker_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, points = await _seed(db_session)
    token = _login(client, manager.email)
    created = client.post(
        "/route-templates", json=_template_payload(points), headers=_auth(token)
    ).json()

    response = client.post(
        f"/route-templates/{created['id']}/materialize",
        json={"route_date": "2026-09-07"},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_materialize_forbidden_for_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, _workers, _points = await _seed(db_session)
    token = _login(client, worker_user.email)

    response = client.post(
        f"/route-templates/{uuid4()}/materialize",
        json={"route_date": "2026-09-07"},
        headers=_auth(token),
    )

    assert response.status_code == 403
