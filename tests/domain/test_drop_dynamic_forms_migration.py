"""The migration that drops the dynamic forms: the four form tables, `executions.form_version_id`
and the two Postgres enum types they used. Its downgrade rebuilds the schema, empty.

Runs on a scratch database (tests/support/migrations.py), starting from the allocation reset like
the other post-reset migration tests. Plain (sync) on purpose: alembic's env.py calls
`asyncio.run()`."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "a9d2c4e6f1b3"
_PREVIOUS = "643c2dd3ca42"

_FORM_TABLES = ("answers", "form_questions", "form_versions", "forms")
_FORM_TYPES = ("question_type", "form_version_status")


def _tables(url: str) -> set[str]:
    rows = run_sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {str(row[0]) for row in rows}


def _execution_columns(url: str) -> set[str]:
    rows = run_sql(
        url,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'executions'",
    )
    return {str(row[0]) for row in rows}


def _types(url: str) -> set[str]:
    rows = run_sql(
        url,
        "SELECT typname FROM pg_type WHERE typname IN ('question_type', 'form_version_status')",
    )
    return {str(row[0]) for row in rows}


def _question_types(url: str) -> list[str]:
    rows = run_sql(url, "SELECT unnest(enum_range(NULL::question_type))::text")
    return [str(row[0]) for row in rows]


def test_drop_forms_migration_removes_form_tables_and_column(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    assert set(_FORM_TABLES) <= _tables(url)
    assert "form_version_id" in _execution_columns(url)
    assert _types(url) == set(_FORM_TYPES)

    command.upgrade(cfg, _REVISION)

    assert not set(_FORM_TABLES) & _tables(url)
    assert "form_version_id" not in _execution_columns(url)
    assert _types(url) == set()

    # The downgrade rebuilds the schema as it was, with no data in it.
    command.downgrade(cfg, _PREVIOUS)

    assert set(_FORM_TABLES) <= _tables(url)
    assert "form_version_id" in _execution_columns(url)
    assert _types(url) == set(_FORM_TYPES)
    assert _question_types(url) == [
        "TEXT",
        "NUMBER",
        "BOOLEAN",
        "SINGLE_CHOICE",
        "MULTI_CHOICE",
        "MEDIA",
    ]
    for table in _FORM_TABLES:
        assert run_sql(url, f"SELECT count(*) FROM {table}") == [(0,)]
