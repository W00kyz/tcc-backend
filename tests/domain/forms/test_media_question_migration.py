"""The MEDIA question type migration: adds the enum value, and its downgrade refuses to drop it
while a MEDIA question exists (that question would have no type left to hold).

Runs on a scratch database (tests/support/migrations.py): the shared one is past the
irreversible allocation reset. Plain (sync) test on purpose: alembic's env.py calls
`asyncio.run()`, which would blow up inside an already-running event loop (Etapa 9 pattern,
tests/domain/monitoring/test_models.py)."""

import pytest
from alembic import command

from tests.support.migrations import LAST_REVERSIBLE_REVISION, alembic_config, run_sql

_BEFORE_MEDIA = "c3e8a5d27f10"


def _question_types(url: str) -> list[str]:
    rows = run_sql(url, "SELECT unnest(enum_range(NULL::question_type))::text")
    return [str(row[0]) for row in rows]


# One statement per call: asyncpg refuses several commands in one prepared statement.
_SEED_MEDIA_QUESTION = (
    "INSERT INTO service_types (id, name, average_duration_minutes) "
    "VALUES ('00000000-0000-0000-0000-00000000aa01', 'Limpeza', 30)",
    "INSERT INTO forms (id, service_type_id, name) VALUES "
    "('00000000-0000-0000-0000-00000000aa02', '00000000-0000-0000-0000-00000000aa01', 'F')",
    "INSERT INTO form_versions (id, form_id, status, version_number) VALUES "
    "('00000000-0000-0000-0000-00000000aa03', '00000000-0000-0000-0000-00000000aa02', "
    "'DRAFT', 0)",
    "INSERT INTO form_questions "
    "(id, form_version_id, stable_key, order_index, prompt, question_type, required, options) "
    "VALUES ('00000000-0000-0000-0000-00000000aa04', '00000000-0000-0000-0000-00000000aa03', "
    "'00000000-0000-0000-0000-00000000aa05', 0, 'Foto', 'MEDIA', true, '[]')",
)


def test_media_question_type_round_trips(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    assert "MEDIA" in _question_types(url)
    for statement in _SEED_MEDIA_QUESTION:
        run_sql(url, statement)
    with pytest.raises(RuntimeError, match="MEDIA"):
        command.downgrade(cfg, _BEFORE_MEDIA)

    run_sql(url, "TRUNCATE service_types CASCADE")
    command.downgrade(cfg, _BEFORE_MEDIA)
    assert "MEDIA" not in _question_types(url)

    command.upgrade(cfg, LAST_REVERSIBLE_REVISION)
    assert "MEDIA" in _question_types(url)
