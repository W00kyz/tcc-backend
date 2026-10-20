from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import Building
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "senha-forte-o-suficiente"


async def _token(client: TestClient, db: AsyncSession, role: UserRole = UserRole.MANAGER) -> str:
    user = User(
        name="Gestora",
        email=f"{role.value.lower()}@pu.ufcg.edu.br",
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    await db.commit()
    response = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    return str(response.json()["access_token"])


async def test_a_building_without_footprint_lists_null_geo_fields(
    client: TestClient, db_session: AsyncSession
) -> None:
    db_session.add(Building(name="Bloco CD", campus_area="CEEI"))
    await db_session.commit()
    token = await _token(client, db_session)

    listing = client.get("/buildings", headers={"Authorization": f"Bearer {token}"})

    [building] = listing.json()
    assert building["footprint"] is None
    assert building["footprint_source"] is None
    assert building["osm_way_id"] is None


SQUARE = [[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]


async def _building(db: AsyncSession) -> str:
    building = Building(name="Bloco CD", campus_area="CEEI")
    db.add(building)
    await db.commit()
    return str(building.id)


async def test_manager_links_and_replaces_a_footprint(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}
    body = {"footprint": {"type": "Polygon", "coordinates": [SQUARE]}, "osm_way_id": 123}

    linked = client.put(f"/buildings/{building_id}/footprint", json=body, headers=auth)
    assert linked.status_code == 200
    assert linked.json()["footprint_source"] == "osm"
    assert linked.json()["osm_way_id"] == 123

    replaced = client.put(
        f"/buildings/{building_id}/footprint", json={**body, "osm_way_id": 456}, headers=auth
    )
    assert replaced.json()["osm_way_id"] == 456


async def test_a_footprint_cannot_be_removed(client: TestClient, db_session: AsyncSession) -> None:
    """Every building keeps a geometry: it can be redrawn, never cleared."""
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}

    response = client.delete(f"/buildings/{building_id}/footprint", headers=auth)

    assert response.status_code == 405


async def test_an_open_ring_is_refused_with_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}
    open_ring = [*SQUARE[:-1], [-35.9, -7.2]]

    response = client.put(
        f"/buildings/{building_id}/footprint",
        json={"footprint": {"type": "Polygon", "coordinates": [open_ring]}, "osm_way_id": 1},
        headers=auth,
    )

    assert response.status_code == 422


async def test_unknown_building_is_404(client: TestClient, db_session: AsyncSession) -> None:
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}
    body = {"footprint": {"type": "Polygon", "coordinates": [SQUARE]}, "osm_way_id": 1}

    response = client.put(
        "/buildings/00000000-0000-0000-0000-000000000000/footprint", json=body, headers=auth
    )

    assert response.status_code == 404


async def test_field_worker_cannot_link_a_footprint(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session, UserRole.FIELD_WORKER)}"}
    body = {"footprint": {"type": "Polygon", "coordinates": [SQUARE]}, "osm_way_id": 1}

    response = client.put(f"/buildings/{building_id}/footprint", json=body, headers=auth)

    assert response.status_code == 403


async def test_linking_is_recorded_in_the_audit_trail(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}
    body = {"footprint": {"type": "Polygon", "coordinates": [SQUARE]}, "osm_way_id": 77}

    client.put(f"/buildings/{building_id}/footprint", json=body, headers=auth)

    actions = (
        await db_session.scalars(
            select(AuditTrail.action).where(AuditTrail.entity_type == "building")
        )
    ).all()
    assert "set_footprint" in actions


async def test_a_non_positive_osm_way_id_is_refused_with_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}

    response = client.put(
        f"/buildings/{building_id}/footprint",
        json={"footprint": {"type": "Polygon", "coordinates": [SQUARE]}, "osm_way_id": 0},
        headers=auth,
    )

    assert response.status_code == 422


async def test_manager_saves_a_hand_drawn_footprint(
    client: TestClient, db_session: AsyncSession
) -> None:
    """An outline drawn in the dashboard has no OSM way behind it."""
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}

    response = client.put(
        f"/buildings/{building_id}/footprint",
        json={"footprint": {"type": "Polygon", "coordinates": [SQUARE]}},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["footprint"] == {"type": "Polygon", "coordinates": [SQUARE]}
    assert response.json()["footprint_source"] == "drawn"
    assert response.json()["osm_way_id"] is None


async def test_redrawing_an_osm_footprint_drops_the_osm_link(
    client: TestClient, db_session: AsyncSession
) -> None:
    building_id = await _building(db_session)
    auth = {"Authorization": f"Bearer {await _token(client, db_session)}"}
    outline = {"type": "Polygon", "coordinates": [SQUARE]}
    client.put(
        f"/buildings/{building_id}/footprint",
        json={"footprint": outline, "osm_way_id": 123},
        headers=auth,
    )

    redrawn = client.put(
        f"/buildings/{building_id}/footprint", json={"footprint": outline}, headers=auth
    )

    assert redrawn.json()["footprint_source"] == "drawn"
    assert redrawn.json()["osm_way_id"] is None
