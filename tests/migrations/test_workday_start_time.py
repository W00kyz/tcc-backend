"""The workday window gets its opening time (spec 2026-11-05-banco-de-testes-horario-expediente
§2): `system_settings.workday_start_time`, 07:00 by default. The end of the day
(`workday_end_time`, 17:00) already existed and the migration must never touch it.

Plain (sync) tests on purpose: alembic's env.py calls `asyncio.run()`. Starts from the allocation
reset, goes up to the previous head and only then applies the migration under test."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "c5e8a2d4f6b1"
_PREVIOUS = "b7d3f9e1c5a2"


def _upgrade_to_previous(url: str) -> None:
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)


def _times(url: str) -> list[tuple[object, ...]]:
    return run_sql(
        url, "SELECT workday_start_time::text, workday_end_time::text FROM system_settings"
    )


def test_upgrade_adds_the_start_and_leaves_the_end_alone(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_to_previous(url)
    assert run_sql(url, "SELECT workday_end_time::text FROM system_settings") == [("17:00:00",)]

    command.upgrade(alembic_config(url), _REVISION)

    assert _times(url) == [("07:00:00", "17:00:00")]


def test_upgrade_keeps_an_end_time_the_admin_edited(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_to_previous(url)
    run_sql(url, "UPDATE system_settings SET workday_end_time = '20:00'")

    command.upgrade(alembic_config(url), _REVISION)

    assert _times(url) == [("07:00:00", "20:00:00")]


def test_downgrade_drops_the_column(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _upgrade_to_previous(url)
    cfg = alembic_config(url)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _PREVIOUS)

    columns = run_sql(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'system_settings' AND column_name LIKE 'workday_%'",
    )
    assert columns == [("workday_end_time",)]
    assert run_sql(url, "SELECT workday_end_time::text FROM system_settings") == [("17:00:00",)]
