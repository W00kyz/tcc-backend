import uuid
from dataclasses import dataclass

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import Building, Floor, Place, ServicePoint, ServiceType
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import ON_CAMPUS, make_service_type

OUTLINE = {
    "type": "Polygon",
    "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]],
}
PASSWORD = "senha-forte-o-suficiente"
UNKNOWN = "00000000-0000-0000-0000-000000000000"


@dataclass
class Setup:
    headers: dict[str, str]
    worker_headers: dict[str, str]
    garden: ServiceType
    cleaning: ServiceType


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user(role: UserRole) -> User:
    return User(
        name="Larissa",
        email=f"{uuid.uuid4().hex[:8]}@pu.ufcg.edu.br",
        password_hash=hash_password(PASSWORD),
        role=role,
    )


async def _setup(client: TestClient, db: AsyncSession) -> Setup:
    manager, worker = _user(UserRole.MANAGER), _user(UserRole.FIELD_WORKER)
    db.add_all([manager, worker])
    garden = await make_service_type(db, company_name="Verde", type_name="Jardinagem")
    cleaning = await make_service_type(db)
    await db.commit()
    return Setup(_login(client, manager.email), _login(client, worker.email), garden, cleaning)


def _create(client: TestClient, setup: Setup, **extra: object) -> dict[str, object]:
    response = client.post(
        "/places",
        json={
            "name": "Praça central",
            "latitude": ON_CAMPUS[0],
            "longitude": ON_CAMPUS[1],
            "service_type_id": str(setup.garden.id),
            **extra,
        },
        headers=setup.headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _points(db: AsyncSession) -> list[ServicePoint]:
    return list(await db.scalars(select(ServicePoint).order_by(ServicePoint.created_at)))


async def test_a_place_is_created_with_its_first_point(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    place = _create(client, setup, name="  Praça central  ")

    assert place["name"] == "Praça central"
    assert (place["latitude"], place["longitude"]) == ON_CAMPUS
    assert set(place) == {"id", "name", "latitude", "longitude"}
    (point,) = await _points(db_session)
    assert (point.name, point.description) == ("Praça central", "")
    assert (point.latitude, point.longitude) == ON_CAMPUS
    assert (point.floor_id, point.room_id) == (None, None)
    assert str(point.place_id) == place["id"]
    assert point.service_type_id == setup.garden.id
    assert point.active


async def test_the_place_creation_is_audited(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)

    place = _create(client, setup)

    entries = list(await db_session.scalars(select(AuditTrail)))
    assert {(entry.entity_type, entry.action) for entry in entries} == {
        ("place", "create"),
        ("service_point", "create"),
    }
    assert str(place["id"]) in {str(entry.entity_id) for entry in entries}


async def test_a_position_outside_the_campus_is_a_422_with_a_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    response = client.post(
        "/places",
        json={
            "name": "Longe",
            "latitude": -8.0,
            "longitude": -35.91,
            "service_type_id": str(setup.garden.id),
        },
        headers=setup.headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "OUTSIDE_CAMPUS"
    assert "-8.0" in response.json()["detail"]["detail"]
    assert await db_session.scalar(select(Place.id)) is None


async def test_a_duplicate_name_is_a_409(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup)

    response = client.post(
        "/places",
        json={
            "name": "Praça central",
            "latitude": ON_CAMPUS[0],
            "longitude": ON_CAMPUS[1],
            "service_type_id": str(setup.cleaning.id),
        },
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert len(await _points(db_session)) == 1


async def test_an_unknown_service_type_is_a_404_and_creates_nothing(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    response = client.post(
        "/places",
        json={
            "name": "Praça",
            "latitude": ON_CAMPUS[0],
            "longitude": ON_CAMPUS[1],
            "service_type_id": UNKNOWN,
        },
        headers=setup.headers,
    )

    assert response.status_code == 404
    assert await db_session.scalar(select(Place.id)) is None


async def test_a_service_type_without_company_is_a_409_and_creates_nothing(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    orphan = ServiceType(name="Pintura", average_duration_minutes=30)
    db_session.add(orphan)
    await db_session.commit()

    response = client.post(
        "/places",
        json={
            "name": "Praça",
            "latitude": ON_CAMPUS[0],
            "longitude": ON_CAMPUS[1],
            "service_type_id": str(orphan.id),
        },
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SERVICE_TYPE_WITHOUT_COMPANY"
    assert await db_session.scalar(select(Place.id)) is None


async def test_bad_names_and_coordinates_are_422_never_500(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    base = {
        "name": "Praça",
        "latitude": ON_CAMPUS[0],
        "longitude": ON_CAMPUS[1],
        "service_type_id": str(setup.garden.id),
    }

    for override in (
        {"name": ""},
        {"name": "   "},
        {"name": "a" * 201},
        {"latitude": 91},
        {"longitude": -181},
        {"latitude": None},
    ):
        response = client.post("/places", json={**base, **override}, headers=setup.headers)
        assert response.status_code == 422, override
    ok = client.post("/places", json={**base, "name": "a" * 200}, headers=setup.headers)
    assert ok.status_code == 201


async def test_places_are_listed_in_registration_order(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, name="B")
    _create(client, setup, name="A")

    response = client.get("/places", headers=setup.headers)

    assert response.status_code == 200
    assert [place["name"] for place in response.json()] == ["B", "A"]


async def test_a_second_service_adds_a_point_to_the_place(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)

    response = client.post(
        f"/places/{place['id']}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["place_id"], body["floor_id"], body["room_id"]) == (place["id"], None, None)
    assert body["name"] == "Praça central"
    assert (body["latitude"], body["longitude"]) == ON_CAMPUS
    assert body["service_type_name"] == "Limpeza"
    assert body["active"] is True
    assert len(await _points(db_session)) == 2


async def test_the_same_service_twice_in_a_place_is_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)

    response = client.post(
        f"/places/{place['id']}/services",
        json={"service_type_id": str(setup.garden.id)},
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PLACE_SERVICE_EXISTS"
    assert len(await _points(db_session)) == 1


async def test_a_service_on_an_unknown_place_or_type_is_a_404_and_without_company_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)
    orphan = ServiceType(name="Pintura", average_duration_minutes=30)
    db_session.add(orphan)
    await db_session.commit()
    url = f"/places/{place['id']}/services"

    unknown_place = client.post(
        f"/places/{UNKNOWN}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )
    unknown_type = client.post(url, json={"service_type_id": UNKNOWN}, headers=setup.headers)
    no_company = client.post(url, json={"service_type_id": str(orphan.id)}, headers=setup.headers)

    assert unknown_place.status_code == 404
    assert unknown_type.status_code == 404
    assert no_company.status_code == 409
    assert len(await _points(db_session)) == 1


async def test_renaming_a_place_renames_all_of_its_points(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)
    client.post(
        f"/places/{place['id']}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )

    response = client.patch(
        f"/places/{place['id']}", json={"name": "Jardim"}, headers=setup.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Jardim"
    db_session.expire_all()
    points = await _points(db_session)
    assert [point.name for point in points] == ["Jardim"] * 2
    assert {(p.latitude, p.longitude) for p in points} == {ON_CAMPUS}
    entries = list(
        await db_session.scalars(select(AuditTrail).where(AuditTrail.action == "update"))
    )
    assert [entry.entity_type for entry in entries] == ["place"]
    assert entries[0].before_data == {
        "name": "Praça central",
        "latitude": ON_CAMPUS[0],
        "longitude": ON_CAMPUS[1],
    }


async def test_moving_a_place_moves_all_of_its_points(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)
    client.post(
        f"/places/{place['id']}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )

    response = client.patch(
        f"/places/{place['id']}",
        json={"latitude": -7.2100, "longitude": -35.9000},
        headers=setup.headers,
    )

    assert response.status_code == 200, response.text
    assert (response.json()["latitude"], response.json()["longitude"]) == (-7.21, -35.9)
    db_session.expire_all()
    points = await _points(db_session)
    assert {(p.latitude, p.longitude) for p in points} == {(-7.21, -35.9)}
    assert {p.name for p in points} == {"Praça central"}


async def test_moving_a_place_outside_the_campus_is_a_422_and_changes_nothing(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)

    response = client.patch(
        f"/places/{place['id']}", json={"latitude": -8.0}, headers=setup.headers
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "OUTSIDE_CAMPUS"
    db_session.expire_all()
    assert {(p.latitude, p.longitude) for p in await _points(db_session)} == {ON_CAMPUS}


async def test_place_updates_that_cannot_apply_are_4xx(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, name="Praça")
    other = _create(client, setup, name="Jardim")
    url = f"/places/{other['id']}"

    duplicate = client.patch(url, json={"name": "Praça"}, headers=setup.headers)
    blank = client.patch(url, json={"name": "  "}, headers=setup.headers)
    long = client.patch(url, json={"name": "a" * 201}, headers=setup.headers)
    empty = client.patch(url, json={}, headers=setup.headers)
    unknown = client.patch(f"/places/{UNKNOWN}", json={"name": "X"}, headers=setup.headers)

    assert duplicate.status_code == 409
    assert isinstance(duplicate.json()["detail"], str)
    assert (blank.status_code, long.status_code, empty.status_code) == (422, 422, 422)
    assert unknown.status_code == 404
    db_session.expire_all()
    assert sorted(p.name for p in await _points(db_session)) == ["Jardim", "Praça"]


async def test_a_stale_name_sent_to_a_place_point_does_not_overwrite_the_place_name(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, name="Praça nova")
    point = (await _points(db_session))[0]

    response = client.patch(
        f"/service-points/{point.id}",
        json={"name": "Nome antigo", "description": "Perto do bar", "active": False},
        headers=setup.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Praça nova"
    assert response.json()["description"] == "Perto do bar"
    assert response.json()["active"] is False
    assert response.json()["floor_id"] is None
    assert response.json()["place_id"] == str(point.place_id)


async def test_service_points_can_be_listed_by_place(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    first = _create(client, setup, name="A")
    _create(client, setup, name="B")

    response = client.get(f"/service-points?place_id={first['id']}", headers=setup.headers)

    assert response.status_code == 200
    assert [(p["name"], p["place_id"]) for p in response.json()] == [("A", first["id"])]


async def test_the_building_filter_ignores_points_of_places(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT", footprint=OUTLINE)
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo", level=0)
    db_session.add(floor)
    await db_session.flush()
    db_session.add(
        ServicePoint(
            floor_id=floor.id,
            service_type_id=setup.cleaning.id,
            name="Sala",
            description="",
            latitude=-7.2145,
            longitude=-35.9085,
        )
    )
    await db_session.commit()
    _create(client, setup)

    by_building = client.get(f"/service-points?building_id={building.id}", headers=setup.headers)
    everything = client.get("/service-points", headers=setup.headers)

    assert [p["name"] for p in by_building.json()] == ["Sala"]
    assert by_building.json()[0]["place_id"] is None
    assert [p["name"] for p in everything.json()] == ["Sala", "Praça central"]


async def test_every_place_route_needs_a_manager(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    place = _create(client, setup)
    url = f"/places/{place['id']}"
    body = {"service_type_id": str(setup.cleaning.id)}
    worker = setup.worker_headers

    responses = [
        client.get("/places", headers=worker),
        client.post(
            "/places",
            json={"name": "X", "latitude": ON_CAMPUS[0], "longitude": ON_CAMPUS[1], **body},
            headers=worker,
        ),
        client.patch(url, json={"name": "X"}, headers=worker),
        client.post(f"{url}/services", json=body, headers=worker),
        client.get(f"{url}/deletion-impact", headers=worker),
        client.delete(url, headers=worker),
    ]

    assert [response.status_code for response in responses] == [403] * 6
    assert client.get("/places").status_code == 401


async def test_changing_a_place_point_to_a_type_its_place_already_has_is_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    garden_id, cleaning_id = setup.garden.id, setup.cleaning.id
    place = _create(client, setup)
    client.post(
        f"/places/{place['id']}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )
    cleaning_point = next(
        p for p in await _points(db_session) if p.service_type_id == setup.cleaning.id
    )

    response = client.patch(
        f"/service-points/{cleaning_point.id}",
        json={"service_type_id": str(setup.garden.id)},
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PLACE_SERVICE_EXISTS"
    db_session.expire_all()
    assert {p.service_type_id for p in await _points(db_session)} == {garden_id, cleaning_id}


async def test_a_place_point_can_keep_its_type_or_move_to_a_free_one(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup)
    point = (await _points(db_session))[0]
    url = f"/service-points/{point.id}"

    same = client.patch(
        url, json={"service_type_id": str(setup.garden.id), "active": False}, headers=setup.headers
    )
    free = client.patch(
        url, json={"service_type_id": str(setup.cleaning.id)}, headers=setup.headers
    )

    assert same.status_code == 200, same.text
    assert free.status_code == 200, free.text
    assert free.json()["service_type_id"] == str(setup.cleaning.id)
