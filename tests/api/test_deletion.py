"""Removing a building or a service point takes its history with it (spec
2026-10-20-predios-pontos-dialogs-design.md §3.4): route stops and everything hanging off them,
routes left with no stop, and — for a building — floors and QR codes. Photo
objects are removed from storage once the rows are gone."""

from typing import cast

from app.core.config import Settings
from app.domain.catalog.models import Building, Floor, ServicePoint
from app.domain.execution.models import EvidenceItem, Execution
from app.domain.identity.models import UserRole
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStop
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import _TWO_ROOMS, SeededRoute, _add_user, _login, _post, _seed_route
from tests.api.test_evidence import _PHOTO_BYTES, _PHOTO_SHA, _upload_photo
from tests.support.object_store import FakeObjectStore

_MANAGER_EMAIL = "gestora@pu.ufcg.edu.br"


def _manager_auth(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, email=_MANAGER_EMAIL)}"}


def _store(client: TestClient) -> FakeObjectStore:
    return cast(FakeObjectStore, cast(FastAPI, client.app).state.object_store)


async def _count(db: AsyncSession, model: type[object]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _executed_first_stop(
    client: TestClient, db_session: AsyncSession, seeded: SeededRoute
) -> None:
    """The worker checks in at the first stop and uploads one photo."""
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    worker_token = _login(client)
    check_in = _post(
        client, worker_token, seeded.qr_code.public_code, route_stop_id=str(seeded.stops[0].id)
    )
    assert check_in.status_code == 201, check_in.text
    execution_id = check_in.json()["execution_id"]
    upload = _upload_photo(client, worker_token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)
    assert upload.status_code == 201, upload.text


async def test_the_impact_counts_what_goes_with_a_point(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    await _executed_first_stop(client, db_session, seeded)

    response = client.get(
        f"/service-points/{seeded.points[0].id}/deletion-impact", headers=_manager_auth(client)
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"points": 1, "floors": 0, "routes": 1, "executions": 1, "photos": 1}


async def test_deleting_a_point_removes_its_history_but_keeps_a_route_with_other_stops(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    # Read before the session is expired below: an expired attribute would lazy-load.
    removed, kept, route_id = seeded.points[0].id, seeded.points[1].id, seeded.route.id
    await _executed_first_stop(client, db_session, seeded)
    assert len(_store(client).objects) == 1

    response = client.delete(f"/service-points/{removed}", headers=_manager_auth(client))

    assert response.status_code == 204, response.text
    db_session.expire_all()
    assert await db_session.get(ServicePoint, removed) is None
    assert await db_session.get(Route, route_id) is not None
    remaining = (
        await db_session.execute(select(RouteStop.service_point_id, RouteStop.order_index))
    ).all()
    # The stop left behind is renumbered to the head of the route.
    assert [tuple(row) for row in remaining] == [(kept, 1)]
    assert await _count(db_session, Execution) == 0
    assert await _count(db_session, EvidenceItem) == 0
    assert _store(client).objects == {}


async def test_deleting_the_only_point_of_a_route_deletes_the_route(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    point_id, route_id = seeded.points[0].id, seeded.route.id
    await _executed_first_stop(client, db_session, seeded)

    response = client.delete(f"/service-points/{point_id}", headers=_manager_auth(client))

    assert response.status_code == 204, response.text
    db_session.expire_all()
    assert await db_session.get(Route, route_id) is None


async def test_deleting_a_building_removes_floors_qr_codes_points_and_routes(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _executed_first_stop(client, db_session, seeded)
    building_id = seeded.floor.building_id
    auth = _manager_auth(client)

    impact = client.get(f"/buildings/{building_id}/deletion-impact", headers=auth)
    response = client.delete(f"/buildings/{building_id}", headers=auth)

    assert impact.json() == {"points": 1, "floors": 1, "routes": 1, "executions": 1, "photos": 1}
    assert response.status_code == 204, response.text
    db_session.expire_all()
    for model in (Building, Floor, QrCode, ServicePoint, Route, RouteStop, Execution):
        assert await _count(db_session, model) == 0, model.__name__
    assert _store(client).objects == {}


async def test_a_photo_already_missing_from_storage_does_not_block_the_deletion(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _executed_first_stop(client, db_session, seeded)
    _store(client).objects.clear()

    response = client.delete(
        f"/buildings/{seeded.floor.building_id}", headers=_manager_auth(client)
    )

    assert response.status_code == 204, response.text


async def test_deletion_answers_404_for_unknown_records_and_403_for_a_field_worker(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, _MANAGER_EMAIL, UserRole.MANAGER)
    auth = _manager_auth(client)
    unknown = "00000000-0000-0000-0000-000000000000"

    assert client.delete(f"/buildings/{unknown}", headers=auth).status_code == 404
    assert client.delete(f"/service-points/{unknown}", headers=auth).status_code == 404
    assert client.get(f"/buildings/{unknown}/deletion-impact", headers=auth).status_code == 404
    worker = {"Authorization": f"Bearer {_login(client)}"}
    assert (
        client.delete(f"/service-points/{seeded.points[0].id}", headers=worker).status_code == 403
    )
