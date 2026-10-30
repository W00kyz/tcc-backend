"""The `COMPLETION_REFUSED` alert kind (spec 2026-10-30-locais-externos §4): the Postgres enum
`alert_kind`, shared by `alerts.kind` and `alert_rules.rule_kind`, gains the value. Plain (sync)
tests on purpose: alembic's env.py calls `asyncio.run()`."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "b7d3f9e1c5a2"
_PREVIOUS = "a4c8e1f7b3d9"
_KIND_COLUMNS = (("alerts", "kind"), ("alert_rules", "rule_kind"))


def _enum_values(url: str) -> list[str]:
    ((values,),) = run_sql(url, "SELECT enum_range(NULL::alert_kind)::text")
    return str(values).strip("{}").split(",")


def test_upgrade_adds_the_value_to_the_shared_enum_and_both_columns_take_it(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    assert "COMPLETION_REFUSED" not in _enum_values(url)

    command.upgrade(cfg, _REVISION)

    assert _enum_values(url)[-1] == "COMPLETION_REFUSED"
    for table, column in _KIND_COLUMNS:
        udt = run_sql(
            url,
            "SELECT udt_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND column_name = '{column}'",
        )
        assert udt == [("alert_kind",)]
    run_sql(url, "INSERT INTO alert_rules (rule_kind, enabled) VALUES ('COMPLETION_REFUSED', true)")
    assert run_sql(
        url, "SELECT count(*) FROM alert_rules WHERE rule_kind = 'COMPLETION_REFUSED'"
    ) == [(1,)]


def test_downgrade_is_a_documented_no_op_and_the_upgrade_can_be_repeated(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _PREVIOUS)
    command.upgrade(cfg, _REVISION)

    # Postgres cannot drop an enum value: it is still there, once.
    assert _enum_values(url).count("COMPLETION_REFUSED") == 1
