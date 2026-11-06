"""The manual-test seed (`python -m app.seed_test`): real outlines around the author's address,
a settings row open 24 h all week, and a route that can start from home at any hour."""

from datetime import UTC, datetime, time

import pytest
from app import seed_test as seed_test_module
from app.core.campus_clock import campus_today
from app.core.config import get_settings
from app.domain.catalog.models import Building, FieldWorker, Floor, Room, ServicePoint
from app.domain.identity.models import User
from app.domain.qr.models import QrCode
from app.domain.routing.allocation import AllocationResult, start_or_resume
from app.domain.routing.models import Route
from app.domain.settings.campus import inside_campus
from app.domain.settings.models import SystemSettings
from app.seed_data import TamborFixture, load_tambor_buildings
from app.seed_data.geometry import point_in_ring
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Sunday 2026-11-08 at 03:00 America/Fortaleza (UTC-3) is 06:00 UTC.
_SUNDAY_3AM = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
# Nominatim's point for the street and its bounding box (the author's house is somewhere on it).
_STREET = (-7.2567, -35.8900)
_STREET_BOX_CORNERS = [
    (lat, lng) for lat in (-7.2570248, -7.2563948) for lng in (-35.8928371, -35.8871367)
]


@pytest.fixture
async def seeded(
    postgres_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncSession:
    """Runs the seed once against the test database. `db_session` truncates every table (the
    settings row included) when the test ends, so the 24 h row cannot leak into other tests."""
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("QR_SIGNING_PRIVATE_KEY_HEX", "11" * 32)
    get_settings.cache_clear()
    try:
        await seed_test_module.seed_test()
    finally:
        get_settings.cache_clear()
    return db_session


async def _settings_row(db: AsyncSession) -> SystemSettings:
    return await db.scalar(select(SystemSettings).execution_options(populate_existing=True))  # type: ignore[return-value]


async def test_seed_test_creates_five_or_more_buildings_with_one_to_three_rooms(
    seeded: AsyncSession,
) -> None:
    buildings = list(await seeded.scalars(select(Building)))
    assert len(buildings) >= 5
    assert {b.campus_area for b in buildings} == {"Tambor (teste)"}
    assert all(b.footprint_source == "osm" and b.osm_way_id for b in buildings)
    for building in buildings:
        rooms = await seeded.scalar(
            select(func.count())
            .select_from(Room)
            .join(Floor, Room.floor_id == Floor.id)
            .where(Floor.building_id == building.id)
        )
        assert rooms is not None, building.name
        assert 1 <= rooms <= 3, building.name


async def test_every_room_has_a_service_point_inside_its_building_outline(
    seeded: AsyncSession,
) -> None:
    rooms = list(await seeded.scalars(select(Room)))
    assert len(rooms) >= 5
    for room in rooms:
        point = await seeded.scalar(select(ServicePoint).where(ServicePoint.room_id == room.id))
        assert point is not None, room.name
        assert point.service_type_id is not None, room.name
        assert point.name == room.name
        floor = await seeded.get_one(Floor, room.floor_id)
        building = await seeded.get_one(Building, floor.building_id)
        assert building.footprint is not None
        ring = building.footprint["coordinates"][0]
        assert point_in_ring(point.latitude, point.longitude, ring), (building.name, room.name)


async def test_points_of_one_building_are_spread_apart(seeded: AsyncSession) -> None:
    rows = (
        await seeded.execute(
            select(Floor.building_id, ServicePoint.latitude, ServicePoint.longitude).join(
                ServicePoint, ServicePoint.floor_id == Floor.id
            )
        )
    ).all()
    assert len({(lat, lng) for _, lat, lng in rows}) == len(rows)


async def test_every_floor_has_one_signed_qr_code(seeded: AsyncSession) -> None:
    floors = await seeded.scalar(select(func.count()).select_from(Floor))
    codes = await seeded.scalar(select(func.count()).select_from(QrCode))
    assert floors == codes
    assert floors is not None
    assert floors >= 5


async def test_settings_row_is_open_24h_all_week_around_the_address(seeded: AsyncSession) -> None:
    row = await _settings_row(seeded)
    fixture = load_tambor_buildings()
    assert row.workday_start_time == time(0, 0, 0)
    assert row.workday_end_time == time(23, 59, 59)
    assert row.service_weekdays == [0, 1, 2, 3, 4, 5, 6]
    assert row.check_radius_meters == 25
    # The rectangle is the fixture bbox padded by about 500 m on every side.
    assert row.campus_min_lat == pytest.approx(fixture.bbox.south - 0.0045, abs=2e-4)
    assert row.campus_max_lat == pytest.approx(fixture.bbox.north + 0.0045, abs=2e-4)
    assert row.campus_min_lng < fixture.bbox.west - 0.0045
    assert row.campus_max_lng > fixture.bbox.east + 0.0045


async def test_campus_rectangle_holds_the_street_the_points_and_the_center(
    seeded: AsyncSession,
) -> None:
    row = await _settings_row(seeded)
    fixture = load_tambor_buildings()
    spots = [_STREET, *_STREET_BOX_CORNERS, (fixture.center.lat, fixture.center.lng)]
    spots += [(p.latitude, p.longitude) for p in await seeded.scalars(select(ServicePoint))]
    outside = [spot for spot in spots if not inside_campus(row, *spot)]
    assert outside == []


async def test_seed_test_is_idempotent(seeded: AsyncSession) -> None:
    get_settings.cache_clear()
    try:
        await seed_test_module.seed_test()
    finally:
        get_settings.cache_clear()

    assert await seeded.scalar(select(func.count()).select_from(User)) == 3
    assert await seeded.scalar(select(func.count()).select_from(Building)) == len(
        load_tambor_buildings().buildings
    )


async def _start(db: AsyncSession, fixture: TamborFixture) -> AllocationResult:
    worker = await db.scalar(select(FieldWorker))
    assert worker is not None
    settings = await _settings_row(db)
    return await start_or_resume(
        db,
        worker=worker,
        today=campus_today(_SUNDAY_3AM),
        now=_SUNDAY_3AM,
        lat=fixture.center.lat,
        lng=fixture.center.lng,
        settings=settings,
        osrm=None,
    )


async def test_a_route_can_start_on_a_sunday_at_3am_at_the_address(seeded: AsyncSession) -> None:
    # Must not raise NotAServiceDay / WorkdayNotStarted / WorkdayOver / OutsideCampus.
    result = await _start(seeded, load_tambor_buildings())

    assert result.route.started_at == _SUNDAY_3AM
    assert any(stop for stop in result.route.stops)


async def test_second_start_the_same_day_reuses_the_route(seeded: AsyncSession) -> None:
    fixture = load_tambor_buildings()
    first = await _start(seeded, fixture)
    second = await _start(seeded, fixture)

    assert second.route.id == first.route.id
    assert await seeded.scalar(select(func.count()).select_from(Route)) == 1


def test_point_in_ring_tells_inside_from_outside() -> None:
    square = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]  # [lng, lat]
    assert point_in_ring(1.0, 1.0, square)
    assert not point_in_ring(3.0, 1.0, square)
    assert not point_in_ring(1.0, -0.5, square)
