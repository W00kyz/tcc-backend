import asyncio
from logging.config import fileConfig

from alembic import context
from app.core.config import get_settings
from app.db import models_registry  # noqa: F401 — imports every model to populate Base.metadata
from app.db.base import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _target_database_url() -> str:
    # config.attributes is a plain scratch dict, never backed by alembic.ini — tests
    # (tests/conftest.py) point migrations at a testcontainer through it. Without this
    # indirection, get_settings() (lru_cache'd, already evaluated once by the time any test
    # fixture runs, since importing app.main built its own settings-derived app singleton)
    # would silently keep returning the real/placeholder DATABASE_URL instead of the
    # container's, no matter what Config the caller passes to `command.upgrade`.
    return config.attributes.get("sqlalchemy_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_target_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _target_database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
