"""RF47-RF52 — `run_alert_scan` driver against the real testcontainer: it turns route/stop/
execution state into `Alert` rows, dedupes on re-run, and honours the per-rule on/off switch and
threshold override from `alert_rules`."""

import uuid
from datetime import UTC, datetime, timedelta

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
from app.domain.monitoring.models import AlertKind, AlertRule, AlertStatus
from app.domain.monitoring.scan import run_alert_scan
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime.now(UTC)


async def _base_graph(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Company -> FieldWorker(+User) -> Building -> Floor -> ServicePoint. Returns
    (field_worker_id, service_point_id)."""
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
    return worker.id, point.id


async def _route(
    db: AsyncSession,
    worker_id: uuid.UUID,
    *,
    status: RouteStatus,
    scheduled_start_at: datetime | None = None,
) -> Route:
    route = Route(
        field_worker_id=worker_id,
        route_date=_NOW.date(),
        status=status,
        scheduled_start_at=scheduled_start_at,
    )
    db.add(route)
    await db.flush()
    return route


async def _stop(
    db: AsyncSession,
    route: Route,
    point_id: uuid.UUID,
    *,
    status: RouteStopStatus,
    expected_arrival_to: datetime | None = None,
) -> RouteStop:
    stop = RouteStop(
        route_id=route.id,
        service_point_id=point_id,
        order_index=1,
        status=status,
        expected_arrival_to=expected_arrival_to,
    )
    db.add(stop)
    await db.flush()
    return stop


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


async def test_late_stop_creates_one_open_alert(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    stop = await _stop(
        db_session,
        route,
        point_id,
        status=RouteStopStatus.PENDING,
        expected_arrival_to=_NOW - timedelta(minutes=45),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert len(created) == 1
    alert = created[0]
    assert alert.kind is AlertKind.LATE_STOP
    assert alert.status is AlertStatus.OPEN
    assert alert.dedup_key == f"LATE_STOP:{stop.id}"
    assert alert.route_id == route.id
    assert alert.route_stop_id == stop.id
    assert "janela de chegada" in alert.description


async def test_second_scan_is_idempotent(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    await _stop(
        db_session,
        route,
        point_id,
        status=RouteStopStatus.PENDING,
        expected_arrival_to=_NOW - timedelta(minutes=45),
    )
    await db_session.commit()

    assert len(await run_alert_scan(db_session, now=_NOW)) == 1
    assert await run_alert_scan(db_session, now=_NOW) == []


async def test_one_pass_can_raise_two_rules(db_session: AsyncSession) -> None:
    worker_id, _ = await _base_graph(db_session)
    # PLANNED route 90 min past its start fires LATE_START (>=15) and WORKER_ABSENT (>=30).
    await _route(
        db_session,
        worker_id,
        status=RouteStatus.PLANNED,
        scheduled_start_at=_NOW - timedelta(minutes=90),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert {a.kind for a in created} == {AlertKind.LATE_START, AlertKind.WORKER_ABSENT}


async def test_disabled_rule_produces_no_alert(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    db_session.add(AlertRule(rule_kind=AlertKind.LATE_STOP, enabled=False))
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    await _stop(
        db_session,
        route,
        point_id,
        status=RouteStopStatus.PENDING,
        expected_arrival_to=_NOW - timedelta(minutes=45),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a for a in created if a.kind is AlertKind.LATE_STOP] == []


async def test_threshold_override_beats_the_global(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    db_session.add(AlertRule(rule_kind=AlertKind.LATE_STOP, threshold_minutes=5))
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    stop = await _stop(
        db_session,
        route,
        point_id,
        status=RouteStopStatus.PENDING,
        # 6 min overdue: past the override (5) but well inside the global default (15).
        expected_arrival_to=_NOW - timedelta(minutes=6),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a.dedup_key for a in created] == [f"LATE_STOP:{stop.id}"]


async def test_manager_manual_done_stop_without_evidence_does_not_alert(
    db_session: AsyncSession,
) -> None:
    worker_id, point_id = await _base_graph(db_session)
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    stop = await _stop(db_session, route, point_id, status=RouteStopStatus.DONE)
    await _execution(
        db_session,
        stop,
        worker_id,
        source=ExecutionSource.MANAGER_MANUAL,
        checked_out_at=_NOW - timedelta(minutes=90),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a for a in created if a.kind is AlertKind.NO_EVIDENCE] == []


async def test_app_done_stop_without_evidence_alerts_no_evidence(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    stop = await _stop(db_session, route, point_id, status=RouteStopStatus.DONE)
    await _execution(
        db_session,
        stop,
        worker_id,
        source=ExecutionSource.APP,
        checked_out_at=_NOW - timedelta(minutes=90),
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a.dedup_key for a in created] == [f"NO_EVIDENCE:{stop.id}"]


async def test_app_done_stop_with_evidence_stays_quiet(db_session: AsyncSession) -> None:
    worker_id, point_id = await _base_graph(db_session)
    route = await _route(db_session, worker_id, status=RouteStatus.IN_PROGRESS)
    stop = await _stop(db_session, route, point_id, status=RouteStopStatus.DONE)
    execution = await _execution(
        db_session,
        stop,
        worker_id,
        source=ExecutionSource.APP,
        checked_out_at=_NOW - timedelta(minutes=90),
    )
    db_session.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.NOTE,
            text_body="feito",
            captured_at=_NOW - timedelta(minutes=90),
        )
    )
    await db_session.commit()

    created = await run_alert_scan(db_session, now=_NOW)

    assert [a for a in created if a.kind is AlertKind.NO_EVIDENCE] == []
