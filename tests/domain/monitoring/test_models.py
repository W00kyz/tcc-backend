import asyncio
from datetime import UTC, datetime

import pytest
from alembic import command
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertSeverity,
    AlertStatus,
    Notification,
    WorkerPosition,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.support.factories import seed_route_with_one_stop
from tests.support.migrations import LAST_REVERSIBLE_REVISION, alembic_config


async def test_alert_round_trips_with_and_without_route_stop(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)

    stop_alert = Alert(
        kind=AlertKind.NO_EVIDENCE,
        severity=AlertSeverity.INFO,
        dedup_key=f"NO_EVIDENCE:{stop.id}",
        description="Stop done without evidence.",
        route_id=stop.route_id,
        route_stop_id=stop.id,
        field_worker_id=worker.id,
    )
    route_alert = Alert(
        kind=AlertKind.BATCH_RELEASED,
        severity=AlertSeverity.WARNING,
        dedup_key=f"BATCH_RELEASED:{stop.route_id}",
        description="Batch released.",
        route_id=stop.route_id,
    )
    day_alert = Alert(
        kind=AlertKind.POINTS_UNSERVED,
        severity=AlertSeverity.WARNING,
        dedup_key="POINTS_UNSERVED:2026-10-20",
        description="Points not served today.",
    )
    db_session.add_all([stop_alert, route_alert, day_alert])
    await db_session.flush()
    for alert in (stop_alert, route_alert, day_alert):
        await db_session.refresh(alert)

    assert stop_alert.status is AlertStatus.OPEN
    assert stop_alert.route_stop_id == stop.id
    assert route_alert.route_stop_id is None
    assert route_alert.field_worker_id is None
    assert day_alert.route_id is None  # a day-level alert belongs to no route
    assert stop_alert.created_at is not None


async def test_dedup_key_is_unique(db_session: AsyncSession) -> None:
    stop, _ = await seed_route_with_one_stop(db_session)
    key = f"NO_EVIDENCE:{stop.id}"

    db_session.add(
        Alert(
            kind=AlertKind.NO_EVIDENCE,
            severity=AlertSeverity.WARNING,
            dedup_key=key,
            description="first",
            route_id=stop.route_id,
        )
    )
    await db_session.flush()

    db_session.add(
        Alert(
            kind=AlertKind.NO_EVIDENCE,
            severity=AlertSeverity.WARNING,
            dedup_key=key,
            description="duplicate",
            route_id=stop.route_id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_notification_round_trips(db_session: AsyncSession) -> None:
    _, worker = await seed_route_with_one_stop(db_session)

    note = Notification(
        user_id=worker.user_id,
        kind="ALERT",
        title="Pontos liberados",
        body="Os pontos do João foram liberados.",
        data={"route_id": str(worker.id)},
    )
    db_session.add(note)
    await db_session.flush()
    await db_session.refresh(note)

    assert note.read_at is None
    assert note.data == {"route_id": str(worker.id)}
    assert note.created_at is not None


async def test_worker_position_round_trips(db_session: AsyncSession) -> None:
    stop, worker = await seed_route_with_one_stop(db_session)

    position = WorkerPosition(
        field_worker_id=worker.id,
        route_id=stop.route_id,
        latitude=-7.21,
        longitude=-35.91,
        reported_at=datetime.now(UTC),
    )
    db_session.add(position)
    await db_session.flush()
    await db_session.refresh(position)

    assert position.accuracy_m is None
    assert position.received_at is not None


_MONITORING_TABLES = ("alerts", "alert_rules", "notifications", "worker_positions")
_MONITORING_ENUMS = ("alert_kind", "alert_severity", "alert_status")


async def _schema_probe(url: str) -> tuple[list[str], list[str], list[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = [
                t
                for t in _MONITORING_TABLES
                if (await conn.scalar(text("SELECT to_regclass(:t)"), {"t": t})) is not None
            ]
            enums = list(
                await conn.scalars(
                    text("SELECT typname FROM pg_type WHERE typname = ANY(:names)"),
                    {"names": list(_MONITORING_ENUMS)},
                )
            )
            rule_kinds: list[str] = []
            if "alert_rules" in tables:
                rule_kinds = list(await conn.scalars(text("SELECT rule_kind FROM alert_rules")))
        return tables, enums, rule_kinds
    finally:
        await engine.dispose()


def test_migration_round_trips_clean_and_seeds_alert_rules(pre_reset_database_url: str) -> None:
    """Downgrading past the Etapa 9 migration leaves no orphaned table or enum; the re-upgrade
    re-seeds the 4 rules of that time (the allocation reset swaps them, see
    tests/migrations/test_allocation_reset.py).

    Targets the Etapa 9 down_revision by id rather than "-1": later migrations sit on top of
    monitoring, so "-1" would reverse the wrong one. Runs on a scratch database
    (tests/support/migrations.py) — the shared one is past the irreversible allocation reset.

    A plain (sync) test on purpose: alembic's env.py calls asyncio.run(), which would blow up
    inside an already-running event loop."""
    url = pre_reset_database_url
    cfg = alembic_config(url)

    command.downgrade(cfg, "1212c573b12e")
    tables, enums, _ = asyncio.run(_schema_probe(url))
    assert tables == []
    assert enums == []

    command.upgrade(cfg, LAST_REVERSIBLE_REVISION)
    tables, enums, rule_kinds = asyncio.run(_schema_probe(url))
    assert sorted(tables) == sorted(_MONITORING_TABLES)
    assert sorted(enums) == sorted(_MONITORING_ENUMS)
    assert sorted(rule_kinds) == ["LATE_START", "LATE_STOP", "NO_EVIDENCE", "WORKER_ABSENT"]
