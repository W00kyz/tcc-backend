"""Points stop having a location of their own: the migration moves every existing point of a
building with an outline to that outline's centre, and leaves points of outline-less (legacy)
buildings where they were.

Plain (sync) test on purpose: alembic's env.py calls `asyncio.run()`, which would blow up
inside an already-running event loop (Etapa 9 pattern, tests/domain/monitoring/test_models.py)."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_BEFORE = "d4f1c2a9b8e7"
_RECTANGLE = (
    '{"type": "Polygon", "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], '
    "[-35.908, -7.215], [-35.909, -7.215], [-35.909, -7.214]]]}"
)


def _alembic_config(url: str) -> Config:
    cfg = Config(
        str(__file__).replace(
            "tests/domain/catalog/test_derive_point_coordinates_migration.py", "alembic.ini"
        )
    )
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def _run(url: str, statement: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


_SEED = (
    "INSERT INTO buildings (id, name, campus_area, footprint) VALUES "
    f"('00000000-0000-0000-0000-0000000000b1', 'Com contorno', 'CCT', '{_RECTANGLE}'), "
    "('00000000-0000-0000-0000-0000000000b2', 'Sem contorno', 'CCT', NULL)",
    "INSERT INTO floors (id, building_id, label) VALUES "
    "('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000b1', 'T'), "
    "('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000b2', 'T')",
    "INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) VALUES "
    "('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000f1', "
    "'Sala', '', -7.0, -35.0), "
    "('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-0000000000f2', "
    "'Sala', '', -7.0, -35.0)",
)


def test_points_move_to_their_building_centre(postgres_url: str) -> None:
    cfg = _alembic_config(postgres_url)
    try:
        command.downgrade(cfg, _BEFORE)
        for statement in _SEED:
            asyncio.run(_run(postgres_url, statement))

        command.upgrade(cfg, "head")

        selected = asyncio.run(
            _run(postgres_url, "SELECT id, latitude, longitude FROM service_points")
        )
        rows = {str(point_id): (lat, lon) for point_id, lat, lon in selected}
        assert rows["00000000-0000-0000-0000-0000000000a1"] == (
            pytest.approx(-7.2145),
            pytest.approx(-35.9085),
        )
        assert rows["00000000-0000-0000-0000-0000000000a2"] == (-7.0, -35.0)
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_run(postgres_url, "TRUNCATE service_points, floors, buildings CASCADE"))
