"""Scratch databases for the migration tests.

The session database sits at head, and head is the automatic-allocation reset, whose downgrade
refuses to run (it wiped the operational data). A migration test therefore never downgrades the
shared database: it gets a database of its own, cloned from a template already migrated to the
last revision that can still be reversed, and dropped afterwards.

Plain (sync) helpers on purpose: alembic's env.py calls `asyncio.run()`, which would blow up
inside an already-running event loop (Etapa 9 pattern, tests/domain/monitoring/test_models.py).
"""

import asyncio
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

# The revision right below the automatic-allocation reset: every migration up to it round-trips.
LAST_REVERSIBLE_REVISION = "e7a2b4c6d8f0"

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # alembic/env.py reads the target from config.attributes, not from the ini file.
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def _execute(url: str, statement: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def run_sql(url: str, statement: str) -> list[tuple[object, ...]]:
    """One statement per call: asyncpg refuses several commands in one prepared statement.

    Example: ``rows = run_sql(url, "SELECT name FROM service_points")``
    """
    return asyncio.run(_execute(url, statement))


async def _execute_autocommit(url: str, statement: str) -> None:
    # CREATE/DROP DATABASE cannot run inside a transaction block.
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _database_url(server_url: str, name: str) -> str:
    return make_url(server_url).set(database=name).render_as_string(hide_password=False)


def create_template_database(server_url: str) -> str:
    """Create a database migrated to `LAST_REVERSIBLE_REVISION`; returns its name."""
    name = f"migration_template_{uuid.uuid4().hex[:8]}"
    asyncio.run(_execute_autocommit(server_url, f'CREATE DATABASE "{name}"'))
    command.upgrade(alembic_config(_database_url(server_url, name)), LAST_REVERSIBLE_REVISION)
    return name


def clone_database(server_url: str, template: str) -> str:
    """Create a fresh copy of `template`; returns its URL."""
    name = f"migration_{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute_autocommit(server_url, f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    return _database_url(server_url, name)


def drop_database(server_url: str, name: str) -> None:
    asyncio.run(_execute_autocommit(server_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
