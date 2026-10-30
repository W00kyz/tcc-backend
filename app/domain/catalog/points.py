"""Where a service point is: its building. A point has no location of its own
(docs/specs/2026-10-20-predios-pontos-dialogs-design.md §3.3). `service_points.latitude/longitude`
stay as columns — route optimisation, impediment proximity, check-in and the app's map read them —
but the server writes them: the centre of the building outline, rewritten when it is redrawn."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.footprint import outline_centroid
from app.domain.catalog.models import Building, Floor, ServicePoint


def point_location(building: Building) -> tuple[float, float] | None:
    """(latitude, longitude) a point of `building` takes, or None while it has no outline.

    Example: ``latitude, longitude = point_location(building)``
    """
    if building.footprint is None:
        return None
    longitude, latitude = outline_centroid(building.footprint)
    return latitude, longitude


async def floor_location(db: AsyncSession, floor: Floor) -> tuple[float, float] | None:
    """(latitude, longitude) a point of `floor` takes: its building's, None without an outline.

    Example: ``location = await floor_location(db, floor)``
    """
    building = await db.get(Building, floor.building_id)
    return None if building is None else point_location(building)


async def place_points_of_building(db: AsyncSession, building: Building) -> None:
    """Moves every point of `building` to its outline's centre (after a redraw).

    Example: ``await place_points_of_building(db, building)``
    """
    location = point_location(building)
    if location is None:
        return
    latitude, longitude = location
    floors = select(Floor.id).where(Floor.building_id == building.id)
    await db.execute(
        update(ServicePoint)
        .where(ServicePoint.floor_id.in_(floors))
        .values(latitude=latitude, longitude=longitude)
        .execution_options(synchronize_session=False)
    )
