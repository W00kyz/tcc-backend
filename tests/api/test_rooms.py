import uuid
from dataclasses import dataclass

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import Building, Floor, Room, ServicePoint, ServiceType
from app.domain.catalog.points import point_location
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_service_type

OUTLINE = {
    "type": "Polygon",
    "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]],
}
PASSWORD = "senha-forte-o-suficiente"


@dataclass
class Setup:
    headers: dict[str, str]
    building: Building
    ground: Floor
    first: Floor
    cleaning: ServiceType | None


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _setup(
    client: TestClient,
    db: AsyncSession,
    *,
    outline: bool = True,
    cleaning: bool = True,
    role: UserRole = UserRole.MANAGER,
) -> Setup:
    user = User(
        name="Larissa",
        email=f"{uuid.uuid4().hex[:8]}@pu.ufcg.edu.br",
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    building = Building(name="Bloco CI", campus_area="CCT", footprint=OUTLINE if outline else None)
    db.add_all([user, building])
    await db.flush()
    ground = Floor(building_id=building.id, label="Térreo", level=0)
    first = Floor(building_id=building.id, label="1", level=1)
    db.add_all([ground, first])
    service_type = await make_service_type(db) if cleaning else None
    await db.commit()
    return Setup(_login(client, user.email), building, ground, first, service_type)


def _create(client: TestClient, setup: Setup, floor: Floor, **extra: object) -> dict[str, object]:
    response = client.post(
        "/rooms", json={"floor_id": str(floor.id), **extra}, headers=setup.headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _points(db: AsyncSession) -> list[ServicePoint]:
    return list(await db.scalars(select(ServicePoint).order_by(ServicePoint.created_at)))


async def test_rooms_are_numbered_and_get_a_cleaning_point(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    created = [_create(client, setup, setup.ground) for _ in range(3)]

    assert [room["name"] for room in created] == ["101", "102", "103"]
    assert {room["cleaning"] for room in created} == {"CREATED"}
    points = await _points(db_session)
    assert [point.name for point in points] == ["101", "102", "103"]
    assert all(point.active and point.description == "" for point in points)
    assert all(point.service_type_id == setup.cleaning.id for point in points if setup.cleaning)
    assert {point.room_id for point in points} == {uuid.UUID(str(r["id"])) for r in created}
    assert {(point.latitude, point.longitude) for point in points} == {
        point_location(setup.building)
    }


async def test_rooms_of_the_first_floor_start_at_201(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    assert _create(client, setup, setup.first)["name"] == "201"


async def test_a_named_room_gives_its_name_to_the_point(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    room = _create(client, setup, setup.ground, name="Auditório Hattori")

    assert room["name"] == "Auditório Hattori"
    assert [point.name for point in await _points(db_session)] == ["Auditório Hattori"]


async def test_a_blank_name_counts_as_omitted(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)

    assert _create(client, setup, setup.ground, name="   ")["name"] == "101"


async def test_without_cleaning_no_point_is_created(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    room = _create(client, setup, setup.ground, with_cleaning=False)

    assert room["cleaning"] == "SKIPPED"
    assert await _points(db_session) == []


async def test_without_a_cleaning_type_the_room_is_created_alone(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session, cleaning=False)

    room = _create(client, setup, setup.ground)

    assert room["cleaning"] == "NO_CLEANING_TYPE"
    assert await _points(db_session) == []
    assert len(list(await db_session.scalars(select(Room)))) == 1


async def test_without_an_outline_the_room_is_created_alone(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session, outline=False)

    room = _create(client, setup, setup.ground)

    assert room["cleaning"] == "NO_OUTLINE"
    assert await _points(db_session) == []
    assert len(list(await db_session.scalars(select(Room)))) == 1


async def test_a_cleaning_type_without_company_does_not_count(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session, cleaning=False)
    db_session.add(ServiceType(name="limpeza", average_duration_minutes=30))
    await db_session.commit()

    assert _create(client, setup, setup.ground)["cleaning"] == "NO_CLEANING_TYPE"


async def test_the_room_creation_is_audited(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)

    room = _create(client, setup, setup.ground)

    entries = list(await db_session.scalars(select(AuditTrail)))
    assert {(entry.entity_type, entry.action) for entry in entries} == {
        ("room", "create"),
        ("service_point", "create"),
    }
    assert str(room["id"]) in {str(entry.entity_id) for entry in entries}


async def test_a_duplicate_name_is_rejected(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, setup.ground, name="Laboratório")

    response = client.post(
        "/rooms",
        json={"floor_id": str(setup.ground.id), "name": "Laboratório"},
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)


async def test_an_unknown_floor_is_a_404(client: TestClient, db_session: AsyncSession) -> None:
    setup = await _setup(client, db_session)

    response = client.post("/rooms", json={"floor_id": str(uuid.uuid4())}, headers=setup.headers)

    assert response.status_code == 404


async def test_the_hundredth_numbered_room_is_a_409_asking_for_a_name(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    db_session.add_all(
        Room(floor_id=setup.ground.id, name=str(100 + number)) for number in range(1, 100)
    )
    await db_session.commit()

    response = client.post("/rooms", json={"floor_id": str(setup.ground.id)}, headers=setup.headers)

    assert response.status_code == 409
    assert "give the room a name" in response.json()["detail"]
    named = client.post(
        "/rooms",
        json={"floor_id": str(setup.ground.id), "name": "Sala extra"},
        headers=setup.headers,
    )
    assert named.status_code == 201


async def test_rooms_are_listed_by_floor_or_building(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, setup.ground)
    _create(client, setup, setup.first)

    by_floor = client.get(f"/rooms?floor_id={setup.first.id}", headers=setup.headers)
    by_building = client.get(f"/rooms?building_id={setup.building.id}", headers=setup.headers)

    assert [room["name"] for room in by_floor.json()] == ["201"]
    assert [room["name"] for room in by_building.json()] == ["101", "201"]
    assert set(by_floor.json()[0]) == {"id", "floor_id", "name"}


async def test_renaming_a_room_renames_its_points(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)

    response = client.patch(
        f"/rooms/{room['id']}", json={"name": "Sala de aula"}, headers=setup.headers
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Sala de aula"
    db_session.expire_all()
    assert [point.name for point in await _points(db_session)] == ["Sala de aula"]
    entries = list(
        await db_session.scalars(select(AuditTrail).where(AuditTrail.action == "update"))
    )
    assert [entry.entity_type for entry in entries] == ["room"]


async def test_renaming_a_room_renames_all_of_its_points(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)
    garden = await _second_type(db_session)
    await db_session.commit()
    client.post(
        f"/rooms/{room['id']}/services",
        json={"service_type_id": str(garden.id)},
        headers=setup.headers,
    )

    client.patch(f"/rooms/{room['id']}", json={"name": "Sala de aula"}, headers=setup.headers)

    db_session.expire_all()
    assert [point.name for point in await _points(db_session)] == ["Sala de aula"] * 2


async def test_a_room_name_longer_than_200_characters_is_a_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    too_long = client.post(
        "/rooms",
        json={"floor_id": str(setup.ground.id), "name": "a" * 201},
        headers=setup.headers,
    )

    assert too_long.status_code == 422
    assert _create(client, setup, setup.ground, name="a" * 200)["name"] == "a" * 200


async def test_renaming_to_a_name_in_use_is_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    _create(client, setup, setup.ground)
    second = _create(client, setup, setup.ground)

    response = client.patch(f"/rooms/{second['id']}", json={"name": "101"}, headers=setup.headers)

    assert response.status_code == 409
    db_session.expire_all()
    assert sorted(point.name for point in await _points(db_session)) == ["101", "102"]


async def test_renaming_an_unknown_room_is_a_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)

    response = client.patch(f"/rooms/{uuid.uuid4()}", json={"name": "X"}, headers=setup.headers)

    assert response.status_code == 404


async def _second_type(db: AsyncSession, *, company: bool = True) -> ServiceType:
    if company:
        return await make_service_type(db, company_name="Verde", type_name="Jardinagem")
    service_type = ServiceType(name="Pintura", average_duration_minutes=30)
    db.add(service_type)
    await db.flush()
    return service_type


async def test_a_second_service_adds_a_point_to_the_room(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)
    garden = await _second_type(db_session)
    await db_session.commit()

    response = client.post(
        f"/rooms/{room['id']}/services",
        json={"service_type_id": str(garden.id)},
        headers=setup.headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["room_id"] == room["id"]
    assert body["active"] is True
    assert body["name"] == "101"
    assert body["service_type_name"] == "Jardinagem"
    assert len(await _points(db_session)) == 2


async def test_the_same_service_twice_in_a_room_is_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)
    assert setup.cleaning is not None

    response = client.post(
        f"/rooms/{room['id']}/services",
        json={"service_type_id": str(setup.cleaning.id)},
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ROOM_SERVICE_EXISTS"
    assert len(await _points(db_session)) == 1


async def test_a_service_type_without_company_is_a_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)
    orphan = await _second_type(db_session, company=False)
    await db_session.commit()

    response = client.post(
        f"/rooms/{room['id']}/services",
        json={"service_type_id": str(orphan.id)},
        headers=setup.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SERVICE_TYPE_WITHOUT_COMPANY"


async def test_a_service_for_an_unknown_room_or_type_is_a_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)

    unknown_room = client.post(
        f"/rooms/{uuid.uuid4()}/services",
        json={"service_type_id": str(uuid.uuid4())},
        headers=setup.headers,
    )
    unknown_type = client.post(
        f"/rooms/{room['id']}/services",
        json={"service_type_id": str(uuid.uuid4())},
        headers=setup.headers,
    )

    assert (unknown_room.status_code, unknown_type.status_code) == (404, 404)


async def test_service_points_carry_room_and_active(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session)
    room = _create(client, setup, setup.ground)

    response = client.get(f"/service-points?building_id={setup.building.id}", headers=setup.headers)

    assert [(p["room_id"], p["active"]) for p in response.json()] == [(room["id"], True)]


async def test_a_field_worker_cannot_use_the_room_endpoints(
    client: TestClient, db_session: AsyncSession
) -> None:
    setup = await _setup(client, db_session, role=UserRole.FIELD_WORKER)
    room_id = uuid.uuid4()
    body = {"floor_id": str(setup.ground.id)}

    responses = [
        client.get("/rooms", headers=setup.headers),
        client.post("/rooms", json=body, headers=setup.headers),
        client.patch(f"/rooms/{room_id}", json={"name": "X"}, headers=setup.headers),
        client.post(
            f"/rooms/{room_id}/services",
            json={"service_type_id": str(uuid.uuid4())},
            headers=setup.headers,
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403, 403]
