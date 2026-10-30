import uuid

from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import Building, Floor
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

OUTLINE = {
    "type": "Polygon",
    "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]],
}


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def test_manager_creates_and_filters_floors_by_building(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building_a = Building(name="Bloco CI", campus_area="CCT")
    building_b = Building(name="Bloco CE", campus_area="CCT")
    db_session.add_all([manager, building_a, building_b])
    await db_session.commit()
    token = _login(client, manager.email)

    create = client.post(
        "/floors",
        json={"building_id": str(building_a.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201

    client.post(
        "/floors",
        json={"building_id": str(building_b.id), "label": "1º Andar"},
        headers={"Authorization": f"Bearer {token}"},
    )

    filtered = client.get(
        f"/floors?building_id={building_a.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["label"] == "Térreo"


async def test_duplicate_floor_label_in_the_same_building_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, building])
    await db_session.commit()
    token = _login(client, manager.email)
    client.post(
        "/floors",
        json={"building_id": str(building.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.post(
        "/floors",
        json={"building_id": str(building.id), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_creating_a_floor_for_an_unknown_building_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding I2.1: an unknown building_id used to be misreported as a
    409 "Floor already exists" (the IntegrityError handler assumed the only possible violation
    was the duplicate-label unique constraint)."""
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    token = _login(client, manager.email)

    response = client.post(
        "/floors",
        json={"building_id": str(uuid.uuid4()), "label": "Térreo"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_floors_keep_registration_order_after_an_edit(
    client: TestClient, db_session: AsyncSession
) -> None:
    # Regression: without ORDER BY, PATCHing a row let Postgres return it last (heap order),
    # so the dashboard's floor tabs reshuffled after a rename.
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, building])
    await db_session.commit()
    auth = {"Authorization": f"Bearer {_login(client, manager.email)}"}
    ids = []
    for label in ["Térreo", "1º Andar", "2º Andar"]:
        created = client.post(
            "/floors", json={"building_id": str(building.id), "label": label}, headers=auth
        )
        ids.append(created.json()["id"])

    client.patch(f"/floors/{ids[0]}", json={"label": "Subsolo"}, headers=auth)
    listing = client.get(f"/floors?building_id={building.id}", headers=auth)

    assert [floor["id"] for floor in listing.json()] == ids


async def _manager_headers(client: TestClient, db_session: AsyncSession) -> dict[str, str]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    return {"Authorization": f"Bearer {_login(client, manager.email)}"}


async def test_floors_created_without_a_label_are_numbered(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.commit()

    floors = [
        client.post("/floors", json={"building_id": str(building.id)}, headers=headers).json()
        for _ in range(4)
    ]

    assert [(f["level"], f["label"]) for f in floors] == [
        (0, "Térreo"),
        (1, "1"),
        (2, "2"),
        (3, "3"),
    ]


async def test_a_new_building_comes_with_its_ground_floor(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    created = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT", "footprint": OUTLINE},
        headers=headers,
    )
    building_id = created.json()["id"]

    listing = client.get(f"/floors?building_id={building_id}", headers=headers).json()
    audits = (
        await db_session.scalars(
            select(AuditTrail).where(
                AuditTrail.entity_type == "floor",
                AuditTrail.action == "create",
                AuditTrail.entity_id == uuid.UUID(listing[0]["id"]),
            )
        )
    ).all()

    assert [(f["label"], f["level"]) for f in listing] == [("Térreo", 0)]
    assert len(audits) == 1


async def test_floors_after_the_automatic_ground_floor_are_numbered_from_one(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building_id = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT", "footprint": OUTLINE},
        headers=headers,
    ).json()["id"]

    labels = [
        client.post("/floors", json={"building_id": building_id}, headers=headers).json()["label"]
        for _ in range(3)
    ]

    assert labels == ["1", "2", "3"]


async def test_a_manual_label_still_gets_the_next_level(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building_id = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT", "footprint": OUTLINE},
        headers=headers,
    ).json()["id"]

    floor = client.post(
        "/floors", json={"building_id": building_id, "label": "Subsolo"}, headers=headers
    ).json()

    assert (floor["label"], floor["level"]) == ("Subsolo", 1)


async def test_floors_are_listed_by_level(client: TestClient, db_session: AsyncSession) -> None:
    headers = await _manager_headers(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.commit()
    db_session.add_all(
        [
            Floor(building_id=building.id, label="2", level=2),
            Floor(building_id=building.id, label="Térreo", level=0),
            Floor(building_id=building.id, label="1", level=1),
        ]
    )
    await db_session.commit()

    listing = client.get(f"/floors?building_id={building.id}", headers=headers).json()

    assert [floor["level"] for floor in listing] == [0, 1, 2]


async def test_an_automatic_label_skips_numbers_already_used_as_labels(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.commit()
    db_session.add_all(
        [
            Floor(building_id=building.id, label="Térreo", level=0),
            Floor(building_id=building.id, label="2", level=1),  # level 1 renamed to "2"
        ]
    )
    await db_session.commit()

    responses = [
        client.post("/floors", json={"building_id": str(building.id)}, headers=headers)
        for _ in range(2)
    ]

    assert [r.status_code for r in responses] == [201, 201]
    assert [(r.json()["level"], r.json()["label"]) for r in responses] == [(3, "3"), (4, "4")]


async def test_a_blank_or_oversized_floor_label_is_a_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.commit()
    body = {"building_id": str(building.id)}

    for label in ("", "   ", "a" * 101):
        response = client.post("/floors", json={**body, "label": label}, headers=headers)
        assert response.status_code == 422, label

    created = client.post("/floors", json={**body, "label": "  Subsolo  "}, headers=headers)
    assert created.status_code == 201
    assert created.json()["label"] == "Subsolo"
    longest = client.post("/floors", json={**body, "label": "a" * 100}, headers=headers)
    assert longest.status_code == 201


async def test_a_blank_or_oversized_label_on_rename_is_a_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.commit()
    floor = Floor(building_id=building.id, label="Térreo", level=0)
    db_session.add(floor)
    await db_session.commit()

    for label in ("", "   ", "a" * 101):
        response = client.patch(f"/floors/{floor.id}", json={"label": label}, headers=headers)
        assert response.status_code == 422, label

    renamed = client.patch(f"/floors/{floor.id}", json={"label": "  Subsolo "}, headers=headers)
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Subsolo"


async def test_removing_the_only_floor_makes_the_next_one_the_ground_floor_again(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    building_id = client.post(
        "/buildings",
        json={"name": "Bloco CI", "campus_area": "CCT", "footprint": OUTLINE},
        headers=headers,
    ).json()["id"]
    only = client.get(f"/floors?building_id={building_id}", headers=headers).json()[0]
    assert client.delete(f"/floors/{only['id']}", headers=headers).status_code == 204

    created = client.post("/floors", json={"building_id": building_id}, headers=headers)

    assert created.status_code == 201
    assert (created.json()["label"], created.json()["level"]) == ("Térreo", 0)
