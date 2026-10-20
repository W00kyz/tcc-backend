"""RF47-RF52 — `run_alert_scan` driver against the real testcontainer: it turns route/stop/
execution state into `Alert` rows, dedupes on re-run, honours the per-rule on/off switch and
threshold override from `alert_rules`, and fans WARNING/CRITICAL alerts out to the managers."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.campus_clock import campus_today
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionSource,
)
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    Notification,
)
from app.domain.monitoring.rules import AlertDraft, AlertThresholds, RouteSnapshot
from app.domain.monitoring.scan import run_alert_scan
from app.domain.routing.models import RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_route

_NOW = datetime.now(UTC)


async def _base_graph(db: AsyncSession) -> tuple[FieldWorker, ServicePoint]:
    """Company -> FieldWorker(+User) -> Building -> Floor -> ServicePoint."""
    user = User(
        name="João",
        email=f"joao-{uuid.uuid4()}@empresa.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj=str(uuid.uuid4().int)[:14])
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([user, company, building])
    await db.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="Sala", latitude=-7.2, longitude=-35.9
    )
    worker = FieldWorker(
        full_name="João da Silva", contractor_company_id=company.id, user_id=user.id
    )
    db.add_all([point, worker])
    await db.flush()
    return worker, point


async def _done_stop(
    db: AsyncSession,
    *,
    route_status: RouteStatus = RouteStatus.IN_PROGRESS,
    source: ExecutionSource = ExecutionSource.APP,
    checked_out_minutes_ago: int = 90,
) -> tuple[RouteStop, Execution]:
    """A DONE stop checked out `checked_out_minutes_ago`, with no evidence yet."""
    worker, point = await _base_graph(db)
    route = await make_route(
        db, worker=worker, day=campus_today(_NOW), points=[point], status=route_status
    )
    stop = route.stops[0]
    stop.status = RouteStopStatus.DONE
    execution = await _execution(
        db,
        stop,
        worker.id,
        source=source,
        checked_out_at=_NOW - timedelta(minutes=checked_out_minutes_ago),
    )
    return stop, execution


async def _execution(
    db: AsyncSession,
    stop: RouteStop,
    worker_id: uuid.UUID,
    *,
    source: ExecutionSource,
    checked_out_at: datetime,
) -> Execution:
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker_id,
        checked_in_at=checked_out_at - timedelta(minutes=20),
        checked_out_at=checked_out_at,
        synced_at=checked_out_at,
        source=source,
        idempotency_key=uuid.uuid4(),
    )
    db.add(execution)
    await db.flush()
    return execution


async def _managers(db: AsyncSession, count: int = 2) -> list[uuid.UUID]:
    """Seed `count` MANAGER/ADMIN users — the recipients of a WARNING/CRITICAL alert."""
    roles = [UserRole.MANAGER, UserRole.ADMIN]
    users = [
        User(
            name=f"Gestor {i}",
            email=f"gestor-{uuid.uuid4()}@empresa.com",
            password_hash=hash_password("senha-forte-o-suficiente"),
            role=roles[i % len(roles)],
        )
        for i in range(count)
    ]
    db.add_all(users)
    await db.flush()
    return [u.id for u in users]


def _warning_rule(
    routes: list[RouteSnapshot],
    _thresholds: AlertThresholds,
    _now: datetime,
    _enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """Stands in for the scan's rule with a WARNING draft per route: the snapshot rule left
    (NO_EVIDENCE) is INFO, and only WARNING/CRITICAL alerts are fanned out to the managers."""
    return [
        AlertDraft(
            kind=AlertKind.BATCH_RELEASED,
            severity=AlertSeverity.WARNING,
            dedup_key=f"BATCH_RELEASED:{route.route_id}",
            description="Pontos liberados.",
            route_id=route.route_id,
        )
        for route in routes
    ]


@pytest.fixture
def warning_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.domain.monitoring.scan.rule_no_evidence", _warning_rule)


async def _notifications(db: AsyncSession) -> list[Notification]:
    return list(await db.scalars(select(Notification)))


async def _raise(*_: object, **__: object) -> list[Notification]:
    raise RuntimeError("notify is broken")


@pytest.mark.usefixtures("warning_rule")
async def test_warning_alert_notifies_every_manager(db_session: AsyncSession) -> None:
    manager_ids = await _managers(db_session, count=3)
    await _done_stop(db_session)
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert len(created) == 1
    notifications = await _notifications(db_session)
    assert {n.user_id for n in notifications} == set(manager_ids)
    assert all(n.kind == "ALERT" for n in notifications)
    assert all(n.data["alert_id"] == str(created[0].id) for n in notifications)


async def test_info_alert_does_not_notify(db_session: AsyncSession) -> None:
    await _managers(db_session)
    await _done_stop(db_session)
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a.kind for a in created] == [AlertKind.NO_EVIDENCE]
    assert await _notifications(db_session) == []


@pytest.mark.usefixtures("warning_rule")
async def test_second_scan_creates_no_new_notifications(db_session: AsyncSession) -> None:
    await _managers(db_session, count=2)
    await _done_stop(db_session)
    await db_session.commit()

    await run_alert_scan(db_session, now=_NOW)
    first_count = len(await _notifications(db_session))
    assert await run_alert_scan(db_session, now=_NOW) == []
    assert len(await _notifications(db_session)) == first_count == 2


@pytest.mark.usefixtures("warning_rule")
async def test_notify_failure_does_not_lose_the_alerts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _managers(db_session)
    await _done_stop(db_session)
    await db_session.commit()
    monkeypatch.setattr("app.domain.monitoring.scan.notify_alert", _raise)

    created = await run_alert_scan(db_session, now=_NOW)

    # The scan swallows the broken notify and still returns the alerts...
    assert len(created) == 1
    created_ids = [a.id for a in created]
    # ...and they are committed: after dropping the identity map the row is still there.
    db_session.expunge_all()
    persisted = list(await db_session.scalars(select(Alert)))
    assert [a.id for a in persisted] == created_ids
    assert await _notifications(db_session) == []


async def test_app_done_stop_without_evidence_alerts_no_evidence(db_session: AsyncSession) -> None:
    stop, _ = await _done_stop(db_session)
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert len(created) == 1
    alert = created[0]
    assert alert.kind is AlertKind.NO_EVIDENCE
    assert alert.status is AlertStatus.OPEN
    assert alert.dedup_key == f"NO_EVIDENCE:{stop.id}"
    assert alert.route_id == stop.route_id
    assert alert.route_stop_id == stop.id


async def test_paused_route_is_still_scanned(db_session: AsyncSession) -> None:
    stop, _ = await _done_stop(db_session, route_status=RouteStatus.PAUSED)
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a.dedup_key for a in created] == [f"NO_EVIDENCE:{stop.id}"]


async def test_second_scan_is_idempotent(db_session: AsyncSession) -> None:
    await _done_stop(db_session)
    await db_session.commit()

    assert len(await run_alert_scan(db_session, now=_NOW)) == 1
    assert await run_alert_scan(db_session, now=_NOW) == []


async def test_disabled_rule_produces_no_alert(db_session: AsyncSession) -> None:
    db_session.add(AlertRule(rule_kind=AlertKind.NO_EVIDENCE, enabled=False))
    await _done_stop(db_session)
    await db_session.commit()

    assert await run_alert_scan(db_session, now=_NOW) == []


async def test_threshold_override_beats_the_global(db_session: AsyncSession) -> None:
    db_session.add(AlertRule(rule_kind=AlertKind.NO_EVIDENCE, threshold_minutes=5))
    # 6 min ago: past the override (5) but well inside the global default (30).
    stop, _ = await _done_stop(db_session, checked_out_minutes_ago=6)
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a.dedup_key for a in created] == [f"NO_EVIDENCE:{stop.id}"]


async def test_appeal_done_stop_without_evidence_does_not_alert(
    db_session: AsyncSession,
) -> None:
    await _done_stop(db_session, source=ExecutionSource.APPEAL)
    await db_session.commit()

    assert await run_alert_scan(db_session, now=_NOW) == []


async def test_app_done_stop_with_evidence_stays_quiet(db_session: AsyncSession) -> None:
    _, execution = await _done_stop(db_session)
    db_session.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.NOTE,
            text_body="feito",
            captured_at=_NOW - timedelta(minutes=90),
        )
    )
    await db_session.commit()

    assert await run_alert_scan(db_session, now=_NOW) == []
