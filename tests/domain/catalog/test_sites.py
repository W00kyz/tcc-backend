import uuid

import pytest
from app.domain.catalog.models import Floor, ServicePoint
from app.domain.catalog.sites import SiteView, site_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.support.factories import make_place_point, make_points, make_service_type


async def _loaded(db: AsyncSession, point: ServicePoint) -> ServicePoint:
    db.expunge_all()
    loaded = await db.scalar(
        select(ServicePoint)
        .where(ServicePoint.id == point.id)
        .options(
            selectinload(ServicePoint.floor).selectinload(Floor.building),
            selectinload(ServicePoint.place),
        )
    )
    assert loaded is not None
    return loaded


async def test_a_point_on_a_floor_sits_at_its_building(db_session: AsyncSession) -> None:
    service_type = await make_service_type(db_session)
    [point] = await make_points(db_session, service_type=service_type, names=["Sala 1"])
    point = await _loaded(db_session, point)

    site = site_of(point)

    assert point.floor is not None
    assert site == SiteView(
        kind="BUILDING",
        site_id=point.floor.building.id,
        name=point.floor.building.name,
        floor_id=point.floor.id,
        floor_label="Térreo",
    )


async def test_a_point_of_a_place_sits_at_the_place_without_floor(
    db_session: AsyncSession,
) -> None:
    service_type = await make_service_type(db_session)
    point = await make_place_point(db_session, service_type=service_type, name="Jardim")
    point = await _loaded(db_session, point)

    site = site_of(point)

    assert point.place is not None
    assert site == SiteView(
        kind="PLACE", site_id=point.place.id, name="Praça central", floor_id=None, floor_label=None
    )


def test_a_point_with_neither_place_nor_floor_names_itself_in_the_error() -> None:
    point = ServicePoint(id=uuid.uuid4(), name="Órfão", description="", latitude=0, longitude=0)

    with pytest.raises(ValueError, match=f"{point.id}.*neither a place nor a floor"):
        site_of(point)
