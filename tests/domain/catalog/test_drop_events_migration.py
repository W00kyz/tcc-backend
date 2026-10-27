"""The events feature (RF22, RF25, RF26) was removed: its migration drops the `events` table
and the point_type/event_id columns, keeps every service point (occasional ones included),
and round-trips cleanly.

Runs on a scratch database (tests/support/migrations.py): the shared one is past the
irreversible allocation reset. Plain (sync) test on purpose: alembic's env.py calls
`asyncio.run()`, which would blow up inside an already-running event loop (Etapa 9 pattern,
tests/domain/monitoring/test_models.py)."""

from alembic import command

from tests.support.migrations import LAST_REVERSIBLE_REVISION, alembic_config, run_sql

_BEFORE_REMOVAL = "b7c4e2f9a1d3"


def _schema(url: str) -> tuple[bool, list[str]]:
    has_events = run_sql(url, "SELECT to_regclass('events')") != [(None,)]
    point_columns = run_sql(
        url,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'service_points'",
    )
    return has_events, [str(row[0]) for row in point_columns]


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


def test_migration_drops_events_but_keeps_occasional_points(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)

    command.downgrade(cfg, _BEFORE_REMOVAL)
    has_events, point_columns = _schema(url)
    assert has_events
    assert {"point_type", "event_id"} <= set(point_columns)
    for statement in _SEED_OCCASIONAL_POINT:
        run_sql(url, statement)

    command.upgrade(cfg, LAST_REVERSIBLE_REVISION)
    has_events, point_columns = _schema(url)
    assert not has_events
    assert not {"point_type", "event_id"} & set(point_columns)
    assert run_sql(url, "SELECT name FROM service_points") == [("Tenda",)]
