import uuid

import pytest
from app.api.point_checks import add_audited_point
from app.domain.catalog.models import Place, ServicePoint, ServiceType
from app.domain.catalog.places import OutsideCampus, ensure_inside_campus
from app.domain.identity.models import User, UserRole
from app.domain.settings.models import SystemSettings
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_service_type

BOUNDS = SystemSettings(
    campus_min_lat=-7.2250,
    campus_max_lat=-7.2050,
    campus_min_lng=-35.9200,
    campus_max_lng=-35.8980,
)


def test_a_position_inside_the_campus_passes() -> None:
    ensure_inside_campus(-7.2150, -35.9100, bounds=BOUNDS)


def test_the_edges_of_the_campus_count_as_inside() -> None:
    ensure_inside_campus(-7.2250, -35.8980, bounds=BOUNDS)


def test_a_position_outside_the_campus_is_refused_with_the_offending_value() -> None:
    with pytest.raises(OutsideCampus, match=r"\(-8.0, -35.91\).*expected"):
        ensure_inside_campus(-8.0, -35.91, bounds=BOUNDS)


async def _actor_and_type(db: AsyncSession) -> tuple[User, ServiceType]:
    actor = User(name="Larissa", email="l@pu.ufcg.edu.br", password_hash="x", role=UserRole.MANAGER)
    db.add(actor)
    return actor, await make_service_type(db)


async def test_a_point_needs_exactly_one_site(db_session: AsyncSession) -> None:
    actor, service_type = await _actor_and_type(db_session)
    place = Place(name="Praça", latitude=-7.215, longitude=-35.91)
    db_session.add(place)
    await db_session.flush()
    common = {
        "name": "x",
        "description": "",
        "service_type": service_type,
        "location": (-7.215, -35.91),
    }

    with pytest.raises(ValueError, match="exactly one"):
        await add_audited_point(db_session, actor, **common)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one"):
        await add_audited_point(
            db_session,
            actor,
            floor_id=uuid.uuid4(),
            place_id=place.id,
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="room"):
        await add_audited_point(
            db_session,
            actor,
            place_id=place.id,
            room_id=uuid.uuid4(),
            **common,  # type: ignore[arg-type]
        )
    assert await db_session.get(ServicePoint, uuid.uuid4()) is None


async def test_a_point_of_a_place_is_created_without_floor(db_session: AsyncSession) -> None:
    actor, service_type = await _actor_and_type(db_session)
    place = Place(name="Praça", latitude=-7.215, longitude=-35.91)
    db_session.add(place)
    await db_session.flush()

    point = await add_audited_point(
        db_session,
        actor,
        place_id=place.id,
        name="Praça",
        description="",
        service_type=service_type,
        location=(-7.215, -35.91),
    )

    assert (point.place_id, point.floor_id, point.room_id) == (place.id, None, None)
