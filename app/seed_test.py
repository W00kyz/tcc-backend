"""Seeds the manual-test database: real building outlines around the author's address (Tambor,
Campina Grande) so the whole flow can be tried from home, at any hour. Run once per fresh test
database: uv run python -m app.seed_test

Safe to re-run: a database whose admin already exists is left untouched. The settings row is the
only difference from production: it is open 24 h, every day, and its campus rectangle is the
fixture bbox padded by 500 m. There is no "test mode" flag in the code; these values live only in
the test database's row. Never run this against production data.
"""

import asyncio
from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import models_registry  # noqa: F401 — every model, so all FKs resolve on flush
from app.db.session import build_engine, build_session_factory
from app.domain.catalog.models import Building, Floor, Room, ServicePoint
from app.domain.identity.models import User
from app.domain.qr.crypto import derive_public_key_hex, sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row
from app.seed import ADMIN_EMAIL, BaseActors, create_base_actors, print_logins
from app.seed_data import TamborBuilding, TamborFixture, load_tambor_buildings
from app.seed_data.geometry import interior_points, pad_bbox

# Houses are small, so the "I'm at the room" tolerance is tighter than the 50 m default.
_TEST_CHECK_RADIUS_METERS = 25


def apply_test_settings(row: SystemSettings, fixture: TamborFixture) -> None:
    """Opens the settings row all day, every day, around the fixture (no flush).

    >>> apply_test_settings(row, load_tambor_buildings())
    """
    box = pad_bbox(fixture.bbox)
    row.workday_start_time = time(0, 0, 0)
    row.workday_end_time = time(23, 59, 59)
    row.service_weekdays = [0, 1, 2, 3, 4, 5, 6]
    row.check_radius_meters = _TEST_CHECK_RADIUS_METERS
    row.campus_min_lat = box.south
    row.campus_max_lat = box.north
    row.campus_min_lng = box.west
    row.campus_max_lng = box.east


async def _add_building(
    db: AsyncSession, spec: TamborBuilding, actors: BaseActors, private_key_hex: str
) -> None:
    building = Building(
        name=spec.name,
        campus_area="Tambor (teste)",
        footprint=spec.footprint,
        footprint_source="osm",
        osm_way_id=spec.osm_way_id,
    )
    db.add(building)
    await db.flush()
    floor = Floor(building_id=building.id, label="Térreo", level=0)
    db.add(floor)
    await db.flush()
    await _add_rooms(db, floor, spec, actors)
    payload = sign_qr_payload(floor_id=floor.id, version=1, private_key_hex=private_key_hex)
    db.add(QrCode(floor_id=floor.id, public_code=payload, secret=b"seeded", version=1))
    await db.flush()


async def _add_rooms(
    db: AsyncSession, floor: Floor, spec: TamborBuilding, actors: BaseActors
) -> None:
    spots = interior_points(spec.footprint["coordinates"][0], len(spec.rooms))
    for name, (lat, lng) in zip(spec.rooms, spots, strict=True):
        room = Room(floor_id=floor.id, name=name)
        db.add(room)
        await db.flush()
        db.add(
            ServicePoint(
                floor_id=floor.id,
                room_id=room.id,
                name=name,
                description="Cômodo da casa (teste)",
                latitude=lat,
                longitude=lng,
                service_type_id=actors.service_type.id,
            )
        )


def _print_flutter_line(fixture: TamborFixture, qr_private_key_hex: str) -> None:
    center = fixture.center
    print(
        "flutter run --dart-define=API_BASE_URL=http://<ip-da-máquina>:8000 "
        f"--dart-define=QR_PUBLIC_KEY_HEX={derive_public_key_hex(qr_private_key_hex)} "
        "--dart-define=CAMPUS_TILES_ASSET=assets/tambor.pmtiles "
        f"--dart-define=CAMPUS_CENTER={center.lat},{center.lng}"
    )


async def seed_test() -> None:
    """Loads the test fixture, or just reprints the logins when it is already there.

    asyncio.run(seed_test())
    """
    settings = get_settings()
    fixture = load_tambor_buildings()
    engine = build_engine(settings.database_url)
    async with build_session_factory(engine)() as db:
        if await db.scalar(select(User).where(User.email == ADMIN_EMAIL)) is not None:
            print("Database already seeded; nothing to do.")
        else:
            actors = await create_base_actors(db)
            for spec in fixture.buildings:
                await _add_building(db, spec, actors, settings.qr_signing_private_key_hex)
            # The row exists after the migrations; `get_settings_row` recreates it otherwise.
            apply_test_settings(await get_settings_row(db), fixture)
            await db.commit()
        print_logins(settings.qr_signing_private_key_hex)
        _print_flutter_line(fixture, settings.qr_signing_private_key_hex)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_test())
