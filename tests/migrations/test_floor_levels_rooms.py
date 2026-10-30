"""Floor levels, rooms and point activation (spec 2026-10-29-andares-salas §3, §7) and the
round trip. Plain (sync) tests on purpose: alembic's env.py calls `asyncio.run()`.

Starts from the allocation reset (like test_appeals.py), goes up to the previous head, seeds
legacy data with raw SQL and only then applies the migration under test."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "e2b7d4a9c6f1"
_PREVIOUS = "d8f2b6a4c1e7"

_B1 = "11111111-1111-1111-1111-111111111111"
_B2 = "22222222-2222-2222-2222-222222222222"
_B3 = "33333333-3333-3333-3333-333333333333"
_F_GROUND = "a0000000-0000-0000-0000-000000000001"
_F_TWO = "a0000000-0000-0000-0000-000000000002"
_F_BASEMENT = "a0000000-0000-0000-0000-000000000003"
_F_B2_ONE = "a0000000-0000-0000-0000-000000000004"


def _seed_legacy_catalog(url: str) -> None:
    for building, name in ((_B1, "B1"), (_B2, "B2"), (_B3, "B3")):
        run_sql(
            url,
            f"INSERT INTO buildings (id, name, campus_area) VALUES ('{building}', '{name}', 'CCT')",
        )
    # Registration order (created_at) is Térreo, 2, Subsolo: Subsolo gets the first free level.
    floors = (
        (_F_GROUND, _B1, "Térreo", "2026-01-01"),
        (_F_TWO, _B1, "2", "2026-01-02"),
        (_F_BASEMENT, _B1, "Subsolo", "2026-01-03"),
        (_F_B2_ONE, _B2, "1", "2026-01-01"),
    )
    for floor_id, building, label, created_at in floors:
        run_sql(
            url,
            "INSERT INTO floors (id, building_id, label, created_at) "
            f"VALUES ('{floor_id}', '{building}', '{label}', '{created_at}')",
        )
    points = ((_F_GROUND, "Sala 1", "A"), (_F_GROUND, "Sala 1", "B"), (_F_BASEMENT, "Garagem", "C"))
    for floor_id, name, description in points:
        run_sql(
            url,
            "INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) "
            f"VALUES (gen_random_uuid(), '{floor_id}', '{name}', '{description}', -7.2, -35.9)",
        )


def _upgrade_with_legacy_data(url: str) -> None:
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    _seed_legacy_catalog(url)
    command.upgrade(cfg, _REVISION)


def test_upgrade_backfills_levels_rooms_and_ground_floors(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_data(url)

    levels = run_sql(
        url,
        "SELECT label, level FROM floors WHERE building_id IN "
        f"('{_B1}', '{_B2}') ORDER BY building_id, level",
    )
    assert levels == [("Térreo", 0), ("2", 2), ("Subsolo", 3), ("1", 1)]

    b3 = run_sql(url, f"SELECT label, level FROM floors WHERE building_id = '{_B3}'")
    assert b3 == [("Térreo", 0)]

    rooms = run_sql(url, f"SELECT name FROM rooms WHERE floor_id = '{_F_GROUND}' ORDER BY name")
    assert rooms == [("Sala 1",), ("Sala 1 (2)",)]

    unlinked = run_sql(
        url, "SELECT count(*) FROM service_points WHERE room_id IS NULL OR NOT active"
    )
    assert unlinked == [(0,)]
    descriptions = run_sql(url, "SELECT description FROM service_points ORDER BY description")
    assert descriptions == [("A",), ("B",), ("C",)]


def test_downgrade_drops_rooms_and_the_new_columns(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_data(url)

    command.downgrade(alembic_config(url), _PREVIOUS)

    assert run_sql(url, "SELECT to_regclass('rooms')") == [(None,)]
    columns = run_sql(
        url,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE (table_name = 'floors' AND column_name = 'level') "
        "OR (table_name = 'service_points' AND column_name IN ('room_id', 'active'))",
    )
    assert columns == []
