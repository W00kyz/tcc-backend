"""The events feature (RF22, RF25, RF26) was removed: its migration drops the `events` table
and the point_type/event_id columns, keeps every service point (occasional ones included),
and round-trips cleanly.

Plain (sync) test on purpose: alembic's env.py calls `asyncio.run()`, which would blow up
inside an already-running event loop (Etapa 9 pattern, tests/domain/monitoring/test_models.py)."""

import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_BEFORE_REMOVAL = "b7c4e2f9a1d3"


def _alembic_config(url: str) -> Config:
    cfg = Config(
        str(__file__).replace("tests/domain/catalog/test_drop_events_migration.py", "alembic.ini")
    )
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def _run(url: str, statement: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement))
            return [str(row[0]) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def _schema(url: str) -> tuple[bool, list[str]]:
    has_events = asyncio.run(_run(url, "SELECT to_regclass('events')")) != ["None"]
    point_columns = asyncio.run(
        _run(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'service_points'",
        )
    )
    return has_events, point_columns


# One statement per call: asyncpg refuses several commands in one prepared statement.
_SEED_OCCASIONAL_POINT = (
    "INSERT INTO events (id, name, valid_from, valid_until) "
    "VALUES ('00000000-0000-0000-0000-0000000000e1', 'Vestibular', "
    "'2026-01-01', '2026-01-02')",
    "INSERT INTO buildings (id, name, campus_area) "
    "VALUES ('00000000-0000-0000-0000-0000000000b1', 'Bloco CI', 'CCT')",
    "INSERT INTO floors (id, building_id, label) VALUES "
    "('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000b1', 'Térreo')",
    "INSERT INTO service_points "
    "(id, floor_id, name, description, latitude, longitude, point_type, event_id) VALUES "
    "('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000f1', "
    "'Tenda', 'Tenda', -7.2, -35.9, 'OCCASIONAL', '00000000-0000-0000-0000-0000000000e1')",
)


def test_migration_drops_events_but_keeps_occasional_points(postgres_url: str) -> None:
    cfg = _alembic_config(postgres_url)

    # The try/finally makes sure a mid-test failure never leaves the shared session
    # container downgraded or holding the seeded rows (they bypass the per-test truncate).
    try:
        command.downgrade(cfg, _BEFORE_REMOVAL)
        has_events, point_columns = _schema(postgres_url)
        assert has_events
        assert {"point_type", "event_id"} <= set(point_columns)
        for statement in _SEED_OCCASIONAL_POINT:
            asyncio.run(_run(postgres_url, statement))

        command.upgrade(cfg, "head")
        has_events, point_columns = _schema(postgres_url)
        assert not has_events
        assert not {"point_type", "event_id"} & set(point_columns)
        names = asyncio.run(_run(postgres_url, "SELECT name FROM service_points"))
        assert names == ["Tenda"]
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_run(postgres_url, "TRUNCATE service_points, floors, buildings CASCADE"))
