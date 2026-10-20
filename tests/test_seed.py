"""The development seed must run on a freshly migrated database — and be safe to re-run.

Regression: the seed imported only some models, so the mapper registry never saw
`route_templates` and the first flush failed with NoReferencedTableError on
`routes.template_id` (broken since Etapa 4, unnoticed because nothing exercised it)."""

import pytest
from app import seed as seed_module
from app.core.config import get_settings
from app.domain.identity.models import User
from app.domain.routing.models import Route
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_seed_populates_a_fresh_database_and_is_idempotent(
    postgres_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("QR_SIGNING_PRIVATE_KEY_HEX", "11" * 32)
    get_settings.cache_clear()
    try:
        await seed_module.seed()
        await seed_module.seed()
    finally:
        get_settings.cache_clear()

    users = await db_session.scalar(select(func.count()).select_from(User))
    assert users == 3
    # Routes come from the automatic allocation, never from the seed.
    assert await db_session.scalar(select(func.count()).select_from(Route)) == 0
