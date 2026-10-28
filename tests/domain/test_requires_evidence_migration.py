"""The migration that adds `requires_evidence` to service types and route stops: the backfill
(only "Limpeza" starts requiring evidence; stops copy their type's value) and its round trip.

Runs on a scratch database (tests/support/migrations.py), from the revision right below it. Plain
(sync) on purpose: alembic's env.py calls `asyncio.run()`."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "c5e7a1d3b9f2"
_PREVIOUS = "a9d2c4e6f1b3"

_CLEANING = "00000000-0000-0000-0000-0000000000d1"
_GARDEN = "00000000-0000-0000-0000-0000000000d2"
_POINT = "00000000-0000-0000-0000-0000000000a1"
_POINT_2 = "00000000-0000-0000-0000-0000000000a2"
_POINT_3 = "00000000-0000-0000-0000-0000000000a3"
_ROUTE = "00000000-0000-0000-0000-0000000000c1"
_STOP_CLEANING = "00000000-0000-0000-0000-0000000000e1"
_STOP_GARDEN = "00000000-0000-0000-0000-0000000000e2"
_STOP_UNTYPED = "00000000-0000-0000-0000-0000000000e3"


def _columns(url: str, table: str) -> set[str]:
    rows = run_sql(
        url, f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
    )
    return {str(row[0]) for row in rows}


def _seed(url: str) -> None:
    run_sql(
        url,
        "INSERT INTO service_types (id, name, average_duration_minutes) VALUES "
        f"('{_CLEANING}', 'Limpeza', 30)",
    )
    run_sql(
        url,
        "INSERT INTO service_types (id, name, average_duration_minutes) VALUES "
        f"('{_GARDEN}', 'Jardinagem', 30)",
    )
    run_sql(
        url,
        "INSERT INTO buildings (id, name, campus_area) VALUES "
        "('00000000-0000-0000-0000-0000000000b1', 'Bloco CI', 'CCT')",
    )
    run_sql(
        url,
        "INSERT INTO floors (id, building_id, label) VALUES "
        "('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000b1', 'T')",
    )
    for point in (_POINT, _POINT_2, _POINT_3):
        run_sql(
            url,
            "INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) "
            f"VALUES ('{point}', '00000000-0000-0000-0000-0000000000f1', '{point[-2:]}', '', "
            "-7.2, -35.9)",
        )
    # Not a real worker: route_stops only needs a route row, and the route needs a worker.
    run_sql(
        url,
        "INSERT INTO users (id, name, email, password_hash, role, is_active) VALUES "
        "('00000000-0000-0000-0000-0000000000e0', 'W', 'w@x.com', 'x', 'FIELD_WORKER', true)",
    )
    run_sql(
        url,
        "INSERT INTO contractor_companies (id, name, cnpj) VALUES "
        "('00000000-0000-0000-0000-0000000000c9', 'Co', '12345678000199')",
    )
    run_sql(
        url,
        "INSERT INTO field_workers (id, full_name, contractor_company_id, user_id, status) VALUES "
        "('00000000-0000-0000-0000-0000000000f9', 'W', '00000000-0000-0000-0000-0000000000c9', "
        "'00000000-0000-0000-0000-0000000000e0', 'APPROVED')",
    )
    run_sql(
        url,
        "INSERT INTO routes (id, field_worker_id, route_date, status) VALUES "
        f"('{_ROUTE}', '00000000-0000-0000-0000-0000000000f9', '2026-10-26', 'IN_PROGRESS')",
    )
    for stop, point, type_id, index in (
        (_STOP_CLEANING, _POINT, f"'{_CLEANING}'", 1),
        (_STOP_GARDEN, _POINT_2, f"'{_GARDEN}'", 2),
        (_STOP_UNTYPED, _POINT_3, "NULL", 3),
    ):
        run_sql(
            url,
            "INSERT INTO route_stops (id, route_id, service_point_id, order_index, "
            "batch_number, service_date, service_type_id, status) VALUES "
            f"('{stop}', '{_ROUTE}', '{point}', {index}, 1, '2026-10-26', {type_id}, 'PENDING')",
        )


def test_requires_evidence_migration_backfills_and_round_trips(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    _seed(url)
    assert "requires_evidence" not in _columns(url, "service_types")
    assert "requires_evidence" not in _columns(url, "route_stops")

    command.upgrade(cfg, _REVISION)

    types = run_sql(url, "SELECT name, requires_evidence FROM service_types ORDER BY name")
    assert types == [("Jardinagem", False), ("Limpeza", True)]
    stops = run_sql(url, "SELECT id::text, requires_evidence FROM route_stops ORDER BY order_index")
    assert stops == [(_STOP_CLEANING, True), (_STOP_GARDEN, False), (_STOP_UNTYPED, False)]

    command.downgrade(cfg, _PREVIOUS)

    assert "requires_evidence" not in _columns(url, "service_types")
    assert "requires_evidence" not in _columns(url, "route_stops")
