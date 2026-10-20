"""The MEDIA question type migration: adds the enum value, and its downgrade refuses to drop it
while a MEDIA question exists (that question would have no type left to hold).

Plain (sync) test on purpose: alembic's env.py calls `asyncio.run()`, which would blow up
inside an already-running event loop (Etapa 9 pattern, tests/domain/monitoring/test_models.py)."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_BEFORE_MEDIA = "c3e8a5d27f10"


def _alembic_config(url: str) -> Config:
    cfg = Config(
        str(__file__).replace("tests/domain/forms/test_media_question_migration.py", "alembic.ini")
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


def _question_types(url: str) -> list[str]:
    return asyncio.run(_run(url, "SELECT unnest(enum_range(NULL::question_type))::text"))


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


def test_media_question_type_round_trips(postgres_url: str) -> None:
    cfg = _alembic_config(postgres_url)
    try:
        assert "MEDIA" in _question_types(postgres_url)
        for statement in _SEED_MEDIA_QUESTION:
            asyncio.run(_run(postgres_url, statement))
        with pytest.raises(RuntimeError, match="MEDIA"):
            command.downgrade(cfg, _BEFORE_MEDIA)

        asyncio.run(_run(postgres_url, "TRUNCATE service_types CASCADE"))
        command.downgrade(cfg, _BEFORE_MEDIA)
        assert "MEDIA" not in _question_types(postgres_url)

        command.upgrade(cfg, "head")
        assert "MEDIA" in _question_types(postgres_url)
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_run(postgres_url, "TRUNCATE service_types CASCADE"))
