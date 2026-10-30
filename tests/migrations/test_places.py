"""Places and the one-site rule on service points (spec 2026-10-30-locais-externos §3, §7) and
the round trip. Plain (sync) tests on purpose: alembic's env.py calls `asyncio.run()`.

Starts from the allocation reset (like test_appeals.py), goes up to the previous head, seeds
legacy data with raw SQL and only then applies the migration under test."""

import pytest
from alembic import command
from sqlalchemy.exc import DBAPIError

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "a4c8e1f7b3d9"
_PREVIOUS = "e2b7d4a9c6f1"

_BUILDING = "11111111-1111-1111-1111-111111111111"
_FLOOR = "a0000000-0000-0000-0000-000000000001"
_ROOM = "b0000000-0000-0000-0000-000000000001"
_PLACE = "c0000000-0000-0000-0000-000000000001"


def _seed_legacy_point(url: str) -> None:
    run_sql(
        url,
        f"INSERT INTO buildings (id, name, campus_area) VALUES ('{_BUILDING}', 'B1', 'CCT')",
    )
    run_sql(
        url, f"INSERT INTO floors (id, building_id, label) VALUES ('{_FLOOR}', '{_BUILDING}', 'T')"
    )
    run_sql(url, f"INSERT INTO rooms (id, floor_id, name) VALUES ('{_ROOM}', '{_FLOOR}', 'Sala')")
    run_sql(
        url,
        "INSERT INTO service_points "
        "(id, floor_id, room_id, name, description, latitude, longitude) "
        f"VALUES (gen_random_uuid(), '{_FLOOR}', '{_ROOM}', 'Sala', 'A', -7.2, -35.9)",
    )


def _upgrade_with_legacy_point(url: str) -> None:
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    _seed_legacy_point(url)
    command.upgrade(cfg, _REVISION)


def _insert_place(url: str) -> None:
    run_sql(
        url,
        "INSERT INTO places (id, name, latitude, longitude) "
        f"VALUES ('{_PLACE}', 'Praça', -7.2, -35.9)",
    )


def _insert_point(url: str, floor_id: str | None, place_id: str | None) -> None:
    floor = f"'{floor_id}'" if floor_id else "NULL"
    place = f"'{place_id}'" if place_id else "NULL"
    run_sql(
        url,
        "INSERT INTO service_points (id, floor_id, place_id, name, description, latitude, "
        f"longitude) VALUES (gen_random_uuid(), {floor}, {place}, 'P', '', -7.2, -35.9)",
    )


def test_upgrade_creates_places_and_frees_the_floor(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)

    assert run_sql(url, "SELECT to_regclass('places')") == [("places",)]
    nullable = run_sql(
        url,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'service_points' AND column_name = 'floor_id'",
    )
    assert nullable == [("YES",)]


def test_existing_points_still_satisfy_the_one_site_rule(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)

    rows = run_sql(
        url,
        "SELECT floor_id IS NOT NULL, room_id IS NOT NULL, place_id IS NULL FROM service_points",
    )
    assert rows == [(True, True, True)]


def test_a_point_may_sit_in_a_place_without_floor_or_room(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)
    _insert_place(url)

    _insert_point(url, floor_id=None, place_id=_PLACE)

    assert run_sql(url, "SELECT count(*) FROM service_points WHERE place_id IS NOT NULL") == [(1,)]


def test_a_point_cannot_have_both_a_place_and_a_floor(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)
    _insert_place(url)

    with pytest.raises(DBAPIError, match="service_points_one_site"):
        _insert_point(url, floor_id=_FLOOR, place_id=_PLACE)


def test_downgrade_drops_places_and_restores_the_floor_requirement(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)

    command.downgrade(alembic_config(url), _PREVIOUS)

    assert run_sql(url, "SELECT to_regclass('places')") == [(None,)]
    columns = run_sql(
        url,
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'service_points' AND column_name IN ('place_id', 'floor_id')",
    )
    assert columns == [("floor_id", "NO")]


def test_downgrade_refuses_while_a_point_has_no_floor(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_with_legacy_point(url)
    _insert_place(url)
    _insert_point(url, floor_id=None, place_id=_PLACE)

    with pytest.raises(RuntimeError, match=r"1 service point\(s\) have no floor"):
        command.downgrade(alembic_config(url), _PREVIOUS)

    assert run_sql(url, "SELECT to_regclass('places')") == [("places",)]
