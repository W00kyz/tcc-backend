import uuid

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import Building, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


# The building outline the points take their location from (centre -35.9085, -7.2145).
OUTLINE: dict[str, object] = {
    "type": "Polygon",
    "coordinates": [
        [
            [-35.909, -7.214],
            [-35.908, -7.214],
            [-35.908, -7.215],
            [-35.909, -7.215],
            [-35.909, -7.214],
        ]
    ],
}


async def _seed_floor(
    db_session: AsyncSession, footprint: dict[str, object] | None = OUTLINE
) -> tuple[User, Floor]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT", footprint=footprint)
    db_session.add_all([manager, building])
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.commit()
    return manager, floor


def _create_point(client: TestClient, token: str, floor_id: object, **extra: object) -> Response:
    return client.post(
        "/service-points",
        json={"floor_id": str(floor_id), "name": "Sala 101", "description": "Aula", **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_a_point_takes_its_building_centroid(
    client: TestClient, db_session: AsyncSession
) -> None:
    """A point has no location of its own: it is where its building is."""
    manager, floor = await _seed_floor(db_session)

    response = _create_point(client, _login(client, manager.email), floor.id)

    assert response.status_code == 201, response.text
    assert response.json()["latitude"] == pytest.approx(-7.2145)
    assert response.json()["longitude"] == pytest.approx(-35.9085)


async def test_coordinates_in_the_body_are_refused(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)

    response = _create_point(
        client, _login(client, manager.email), floor.id, latitude=-7.2, longitude=-35.9
    )

    assert response.status_code == 422


async def test_a_point_needs_a_building_outline(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session, footprint=None)

    response = _create_point(client, _login(client, manager.email), floor.id)

    assert response.status_code == 422
    assert "outline" in response.json()["detail"]


async def test_creating_a_service_point_for_an_unknown_floor_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.2."""
    manager, _floor = await _seed_floor(db_session)

    response = _create_point(client, _login(client, manager.email), uuid.uuid4())

    assert response.status_code == 404


async def test_redrawing_the_outline_moves_its_points(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    point_id = _create_point(client, token, floor.id).json()["id"]
    moved = {
        "type": "Polygon",
        "coordinates": [[[-35.92, -7.22], [-35.91, -7.22], [-35.91, -7.23], [-35.92, -7.22]]],
    }

    client.put(
        f"/buildings/{floor.building_id}/footprint",
        json={"footprint": moved},
        headers={"Authorization": f"Bearer {token}"},
    )

    [point] = [
        p
        for p in client.get(
            f"/service-points?floor_id={floor.id}", headers={"Authorization": f"Bearer {token}"}
        ).json()
        if p["id"] == point_id
    ]
    centroid = (-35.913333, -7.223333)  # of that triangle
    assert point["longitude"] == pytest.approx(centroid[0], abs=1e-5)
    assert point["latitude"] == pytest.approx(centroid[1], abs=1e-5)


async def test_editing_a_point_changes_only_its_name_and_description(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    point_id = _create_point(client, token, floor.id).json()["id"]

    response = client.patch(
        f"/service-points/{point_id}",
        json={"name": "Sala 102", "description": "Laboratório"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Sala 102"
    assert response.json()["latitude"] == pytest.approx(-7.2145)


async def test_the_events_endpoints_are_gone(client: TestClient, db_session: AsyncSession) -> None:
    manager, _floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.get("/events", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404


async def test_service_points_can_be_listed_by_building(
    client: TestClient, db_session: AsyncSession
) -> None:
    """The building page lists every point of the building, across its floors."""
    manager, floor = await _seed_floor(db_session)
    upper = Floor(building_id=floor.building_id, label="1º andar")
    other_building = Building(name="Bloco CD", campus_area="CEEI")
    db_session.add_all([upper, other_building])
    await db_session.flush()
    other_floor = Floor(building_id=other_building.id, label="Térreo")
    db_session.add(other_floor)
    await db_session.flush()
    db_session.add_all(
        [
            ServicePoint(
                floor_id=floor.id, name="Sala 101", description="", latitude=-7.2, longitude=-35.9
            ),
            ServicePoint(
                floor_id=upper.id, name="Sala 201", description="", latitude=-7.2, longitude=-35.9
            ),
            ServicePoint(
                floor_id=other_floor.id,
                name="Sala CD",
                description="",
                latitude=-7.2,
                longitude=-35.9,
            ),
        ]
    )
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.get(
        f"/service-points?building_id={floor.building_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert sorted(point["name"] for point in response.json()) == ["Sala 101", "Sala 201"]
