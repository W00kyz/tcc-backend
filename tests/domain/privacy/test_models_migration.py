"""Etapa 11 (RNF10): the LGPD migration creates the privacy tables, adds the two
`system_settings` retention columns, seeds privacy notice v1, and round-trips cleanly.

`test_migration_round_trips...` is a plain (sync) test on purpose: alembic's env.py calls
`asyncio.run()`, which would blow up inside an already-running event loop (Etapa 9 pattern,
tests/domain/monitoring/test_models.py)."""

import asyncio
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from app.domain.privacy.models import PrivacyNotice
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

_PRIVACY_TABLES = ("privacy_notices", "consent_records")


def _alembic_config(url: str) -> Config:
    cfg = Config(
        str(__file__).replace("tests/domain/privacy/test_models_migration.py", "alembic.ini")
    )
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def _probe(url: str) -> tuple[list[str], list[str], list[tuple[int, str, bool]]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = [
                t
                for t in _PRIVACY_TABLES
                if (await conn.scalar(text("SELECT to_regclass(:t)"), {"t": t})) is not None
            ]
            settings_cols = list(
                await conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'system_settings'"
                    )
                )
            )
            notices: list[tuple[int, str, bool]] = []
            if "privacy_notices" in tables:
                rows = await conn.execute(
                    text("SELECT version, body_markdown, is_current FROM privacy_notices")
                )
                notices = [(r[0], r[1], r[2]) for r in rows]
        return tables, settings_cols, notices
    finally:
        await engine.dispose()


def test_migration_round_trips_and_seeds_notice_v1(postgres_url: str) -> None:
    cfg = _alembic_config(postgres_url)

    command.downgrade(cfg, "-1")
    tables, settings_cols, _ = asyncio.run(_probe(postgres_url))
    assert tables == []
    assert "worker_position_retention_days" not in settings_cols
    assert "evidence_retention_days" not in settings_cols

    command.upgrade(cfg, "head")
    tables, settings_cols, notices = asyncio.run(_probe(postgres_url))
    assert sorted(tables) == sorted(_PRIVACY_TABLES)
    assert "worker_position_retention_days" in settings_cols
    assert "evidence_retention_days" in settings_cols

    current = [n for n in notices if n[2]]
    assert len(current) == 1
    assert current[0][0] == 1  # version
    assert current[0][1]  # body_markdown non-empty


async def test_only_one_current_notice_allowed(db_session: AsyncSession) -> None:
    # Truncation between tests may have removed the seeded v1 row, so seed our own baseline.
    await db_session.execute(text("DELETE FROM privacy_notices"))
    db_session.add(
        PrivacyNotice(
            version=10,
            title="baseline",
            body_markdown="x",
            published_at=datetime.now(UTC),
            is_current=True,
        )
    )
    await db_session.flush()

    db_session.add(
        PrivacyNotice(
            version=11,
            title="second current",
            body_markdown="y",
            published_at=datetime.now(UTC),
            is_current=True,
        )
    )
    with pytest.raises(IntegrityError):  # partial unique index on is_current
        await db_session.flush()
