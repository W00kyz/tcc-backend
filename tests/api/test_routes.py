"""Route reads: the manager list/detail and the worker's "rota do dia". Routes are created by the
automatic allocation; these tests seed them with `make_route`, the shape that allocation leaves."""

from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from app.api.deps import get_campus_today
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.forms.models import QuestionType
from app.domain.forms.service import add_question, get_or_create_form, publish_form
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import ReleaseReason, RouteStatus, RouteStopStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_route, make_worker

_PASSWORD = "senha-forte-o-suficiente"


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_actors_and_points(
    db_session: AsyncSession,
) -> tuple[User, User, list[FieldWorker], list[ServicePoint]]:
    """Manager + two field workers (the first one has an app login) + three service points on
    one floor."""
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
    # full_name deliberately differs from worker_user.name: field_worker_name must come from
    # FieldWorker.full_name, not User.name.
    workers = [
        FieldWorker(
            full_name="João da Silva",
            contractor_company_id=company.id,
            user_id=worker_user.id,
            status=FieldWorkerStatus.APPROVED,
        ),
    ]
    db_session.add_all([*points, *workers])
    workers.append(await make_worker(db_session, company=company, full_name="Maria Souza"))
    await db_session.commit()
    return manager, worker_user, workers, points


# --- GET /routes/me (RF27 "rota do dia") ----------------------------------------------


async def test_field_worker_sees_only_their_own_route(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    own = await make_route(db_session, worker=workers[0], day=today_on_campus(), points=points[:1])
    await make_route(db_session, worker=workers[1], day=today_on_campus(), points=points[1:2])
    await db_session.commit()
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    route = response.json()["route"]
    assert route["id"] == str(own.id)
    assert len(route["stops"]) == 1
    assert route["field_worker_name"] == "João da Silva"


async def test_routes_me_is_todays_route_with_geometry(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    today_route = await make_route(
        db_session, worker=workers[0], day=today_on_campus(), points=points[:2]
    )
    today_route.stops[1].leg_geometry = [[-35.899, -7.199], [-35.898, -7.199]]
    await make_route(
        db_session,
        worker=workers[0],
        day=today_on_campus() - timedelta(days=1),
        points=points[2:],
    )
    await db_session.commit()
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    route = response.json()["route"]
    assert route["id"] == str(today_route.id)  # yesterday's is excluded
    assert isinstance(route["stops"][1]["leg_geometry"], list)
    assert route["routing_degraded"] is False
    assert route["current_batch"] == 1


async def test_route_stop_out_carries_batch_and_release_reason(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    route = await make_route(
        db_session, worker=workers[0], day=today_on_campus(), points=points[:2]
    )
    route.stops[1].status = RouteStopStatus.RELEASED
    route.stops[1].release_reason = ReleaseReason.LEFT_CAMPUS
    await db_session.commit()
    token = _login(client, worker_user.email)

    stops = client.get("/routes/me", headers=_auth(token)).json()["route"]["stops"]

    assert [stop["batch_number"] for stop in stops] == [1, 1]
    assert [stop["release_reason"] for stop in stops] == [None, "LEFT_CAMPUS"]
    assert stops[1]["status"] == "RELEASED"
    assert not {"expected_arrival_from", "reassigned", "assignment_history"} & set(stops[0])


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

    route = await make_route(db_session, worker=workers[0], day=today_on_campus(), points=points)
    route.stops[0].service_type_id = published_type.id
    route.stops[2].service_type_id = draft_only_type.id
    await db_session.commit()
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.status_code == 200
    stops = response.json()["route"]["stops"]

    assert stops[0]["service_type_name"] == "Limpeza"
    assert stops[0]["service_type_id"] == str(published_type.id)
    assert stops[0]["form"]["form_version_id"] == str(published_version.id)
    assert len(stops[0]["form"]["questions"][0]["content_hash"]) == 64

    assert stops[1]["service_type_id"] is None
    assert stops[1]["service_type_name"] is None
    assert stops[1]["form"] is None

    assert stops[2]["service_type_name"] == "Jardinagem"
    assert stops[2]["form"] is None  # only a draft version exists


async def test_routes_me_follows_the_campus_day_not_the_server_date(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression: `/routes/me` used `date.today()`, the UTC date in the API container, so from
    21:00 in Campina Grande the worker's app showed tomorrow's routes instead of today's."""
    _manager, worker_user, workers, points = await _seed_actors_and_points(db_session)
    campus_day = today_on_campus() + timedelta(days=1)
    await make_route(db_session, worker=workers[0], day=today_on_campus(), points=points[:1])
    campus_day_route = await make_route(
        db_session, worker=workers[0], day=campus_day, points=points[1:2]
    )
    await db_session.commit()
    cast(FastAPI, client.app).dependency_overrides[get_campus_today] = lambda: campus_day
    token = _login(client, worker_user.email)

    response = client.get("/routes/me", headers=_auth(token))

    assert response.json()["route"]["id"] == str(campus_day_route.id)


# --- GET /routes, GET /routes/{id} (manager) -------------------------------------------


async def test_list_routes_filters_by_date_worker_and_status(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, workers, points = await _seed_actors_and_points(db_session)
    yesterday = today_on_campus() - timedelta(days=1)
    first = await make_route(
        db_session, worker=workers[0], day=yesterday, points=points[:1], status=RouteStatus.DONE
    )
    second = await make_route(
        db_session, worker=workers[1], day=today_on_campus(), points=points[1:2]
    )
    await db_session.commit()
    token = _login(client, manager.email)

    by_date = client.get(f"/routes?date={yesterday.isoformat()}", headers=_auth(token)).json()
    assert [route["id"] for route in by_date] == [str(first.id)]

    by_worker = client.get(f"/routes?field_worker_id={workers[1].id}", headers=_auth(token)).json()
    assert [route["id"] for route in by_worker] == [str(second.id)]

    by_status = client.get("/routes?status=IN_PROGRESS", headers=_auth(token)).json()
    assert [route["id"] for route in by_status] == [str(second.id)]

    unfiltered = client.get("/routes", headers=_auth(token)).json()
    assert {route["id"] for route in unfiltered} == {str(first.id), str(second.id)}


async def test_list_routes_invalid_status_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.get("/routes?status=PLANNED", headers=_auth(token))

    assert response.status_code == 422
    assert "PLANNED" in response.json()["detail"]


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
    route = await make_route(
        db_session, worker=workers[0], day=today_on_campus(), points=points[:2]
    )
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.get(f"/routes/{route.id}", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(route.id)
    assert body["status"] == "IN_PROGRESS"
    assert len(body["stops"]) == 2
    assert not {"route_type", "template_id", "scheduled_start_at"} & set(body)


async def test_field_worker_cannot_read_manager_routes(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, worker_user.email)

    assert client.get("/routes", headers=_auth(token)).status_code == 403
    assert client.get(f"/routes/{uuid4()}", headers=_auth(token)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/routes"),
        ("POST", "/routes/occasional"),
        ("PATCH", "/routes/{id}"),
        ("POST", "/routes/{id}/optimize"),
        ("POST", "/routes/{id}/reassign"),
        ("POST", "/routes/{id}/cancel"),
        ("POST", "/routes/{id}/start"),
        ("POST", "/routes/{id}/stops/{id}/complete-manually"),
        ("GET", "/route-templates"),
    ],
)
async def test_manual_planning_endpoints_are_gone(
    client: TestClient, db_session: AsyncSession, method: str, path: str
) -> None:
    manager, _worker_user, _workers, _points = await _seed_actors_and_points(db_session)
    token = _login(client, manager.email)

    response = client.request(method, path.replace("{id}", str(uuid4())), headers=_auth(token))

    assert response.status_code in (404, 405)
