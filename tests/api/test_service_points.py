import uuid
from datetime import date, timedelta

from app.core.security import hash_password
from app.domain.catalog.models import Building, Event, Floor
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def _seed_floor(db_session: AsyncSession) -> tuple[User, Floor]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, building])
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.commit()
    return manager, floor


async def test_manager_creates_a_regular_service_point(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/service-points",
        json={
            "floor_id": str(floor.id),
            "name": "Sala 101",
            "description": "Sala de aula",
            "latitude": -7.2,
            "longitude": -35.9,
            "point_type": "REGULAR",
            "event_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    assert response.json()["point_type"] == "REGULAR"


async def test_creating_a_service_point_for_an_unknown_floor_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.2."""
    manager, _floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/service-points",
        json={
            "floor_id": str(uuid.uuid4()),
            "name": "Sala 101",
            "description": "Sala de aula",
            "latitude": -7.2,
            "longitude": -35.9,
            "point_type": "REGULAR",
            "event_id": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_creating_an_occasional_point_for_an_unknown_event_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.2."""
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        "/service-points",
        json={
            "floor_id": str(floor.id),
            "name": "Tenda de Inscrição",
            "description": "Tenda temporária",
            "latitude": -7.2,
            "longitude": -35.9,
            "point_type": "OCCASIONAL",
            "event_id": str(uuid.uuid4()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_an_occasional_point_past_its_window_is_hidden_by_default_but_reportable(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    past_event = Event(
        name="Vestibular 2026",
        valid_from=date.today() - timedelta(days=10),
        valid_until=date.today() - timedelta(days=1),
    )
    db_session.add(past_event)
    await db_session.commit()
    token = _login(client, manager.email)
    client.post(
        "/service-points",
        json={
            "floor_id": str(floor.id),
            "name": "Tenda de Inscrição",
            "description": "Tenda temporária",
            "latitude": -7.2,
            "longitude": -35.9,
            "point_type": "OCCASIONAL",
            "event_id": str(past_event.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    default_listing = client.get(
        f"/service-points?floor_id={floor.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert default_listing.json() == []

    full_listing = client.get(
        f"/service-points?floor_id={floor.id}&include_archived=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert len(full_listing.json()) == 1
    assert full_listing.json()[0]["name"] == "Tenda de Inscrição"


async def test_manager_promotes_an_occasional_point_to_regular(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    event = Event(name="Semana de Calouros", valid_from=date.today(), valid_until=date.today())
    db_session.add(event)
    await db_session.commit()
    token = _login(client, manager.email)
    created = client.post(
        "/service-points",
        json={
            "floor_id": str(floor.id),
            "name": "Recepção Calouros",
            "description": "Ponto de recepção",
            "latitude": -7.2,
            "longitude": -35.9,
            "point_type": "OCCASIONAL",
            "event_id": str(event.id),
        },
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    response = client.post(
        f"/service-points/{created['id']}/promote",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["point_type"] == "REGULAR"
    # event_id preserved as historical origin (spec Ruling 2), not cleared:
    assert response.json()["event_id"] == str(event.id)
