"""Shared fixtures: one Postgres/PostGIS testcontainer per test session (spec §8 — no SQLite,
PostGIS is a requirement), a fresh app per test, and truncate-based isolation between tests."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.db.base import Base
from app.db.session import build_engine, build_session_factory
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

# app.main builds a module-level FastAPI singleton at import time (`app = create_app()`), which
# needs Settings() to construct even though no test touches that singleton directly — every test
# gets its own app from the `client` fixture below, built with test_settings against the
# testcontainer. These placeholders only need to satisfy pydantic-settings' required fields.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder"
)
os.environ.setdefault("JWT_SECRET_KEY", "placeholder-see-test-settings-fixture-for-real-value")
os.environ.setdefault("QR_SIGNING_PRIVATE_KEY_HEX", "00" * 32)

from app.main import create_app

_POSTGIS_IMAGE = (
    "postgis/postgis:17-3.5@sha256:83e9999dc3ad8390c210e76130c3a16365ef4f957bb55200d22b7937cfbcb321"
)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(image=_POSTGIS_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        alembic_cfg = Config(str(__file__).replace("tests/conftest.py", "alembic.ini"))
        # alembic/env.py reads this from config.attributes, not from the ini-backed
        # sqlalchemy.url option — see the comment on _target_database_url() there for why.
        alembic_cfg.attributes["sqlalchemy_url"] = url
        command.upgrade(alembic_cfg, "head")
        yield url


@pytest.fixture
def test_settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        jwt_secret_key="test-secret-do-not-use-in-production",
        qr_signing_private_key_hex="11" * 32,  # 32 bytes, deterministic test key
        mail_smtp_host="localhost",
        mail_smtp_port=1,  # no automated test should actually be able to connect here
    )


@pytest_asyncio.fixture
async def db_session(test_settings: Settings) -> AsyncIterator[object]:
    engine = build_engine(test_settings.database_url)
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        yield session
    # Test isolation: truncate everything in reverse FK order.
    async with session_factory() as cleanup:
        for table in reversed(Base.metadata.sorted_tables):
            await cleanup.execute(table.delete())
        await cleanup.commit()
    await engine.dispose()


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings=test_settings)
    with TestClient(app) as test_client:
        yield test_client
