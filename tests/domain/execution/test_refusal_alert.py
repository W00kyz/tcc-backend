"""The COMPLETION_REFUSED alert (spec 2026-10-30-locais-externos §4): a refused completion by
location warns the managers once per stop and day, unless the rule is switched off. The
recorder only flushes — the caller commits."""

import uuid
from dataclasses import dataclass
from datetime import date

from app.core.security import hash_password
from app.domain.catalog.models import FieldWorker
from app.domain.execution.refusal_alert import RefusalReason, record_completion_refusal
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    Notification,
)
from app.domain.monitoring.push import LoggingPushSender
from app.domain.routing.models import Route, RouteStop
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_place_point, make_route, make_service_type, make_worker

_DAY = date(2026, 10, 30)


@dataclass
class _Seed:
    worker: FieldWorker
    route: Route
    stop: RouteStop


async def _seed(db: AsyncSession) -> _Seed:
    service_type = await make_service_type(db)
    assert service_type.contractor_company is not None
    worker = await make_worker(db, company=service_type.contractor_company)
    point = await make_place_point(
        db, service_type=service_type, name="Jardim", place_name="Praça central"
    )
    route = await make_route(db, worker=worker, day=today_on_campus(), points=[point])
    await db.commit()
    return _Seed(worker=worker, route=route, stop=route.stops[0])


async def _manager(db: AsyncSession) -> User:
    manager = User(
        name="Larissa",
        email=f"{uuid.uuid4().hex[:8]}@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db.add(manager)
    await db.flush()
    return manager


async def _refuse(
    db: AsyncSession,
    seed: _Seed,
    *,
    reason: RefusalReason = "OUT_OF_RADIUS",
    distance_m: float | None = 120.4,
    campus_day: date = _DAY,
) -> Alert | None:
    return await record_completion_refusal(
        db,
        stop=seed.stop,
        worker=seed.worker,
        reason=reason,
        distance_m=distance_m,
        radius_m=50,
        campus_day=campus_day,
        push=LoggingPushSender(),
    )


async def _count(db: AsyncSession, model: type) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_a_far_refusal_opens_a_warning_alert_and_tells_the_managers(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    manager = await _manager(db_session)

    alert = await _refuse(db_session, seed)

    assert alert is not None
    assert alert.kind is AlertKind.COMPLETION_REFUSED
    assert alert.severity is AlertSeverity.WARNING
    assert alert.status is AlertStatus.OPEN
    assert alert.route_stop_id == seed.stop.id
    assert alert.field_worker_id == seed.worker.id
    assert alert.route_id == seed.route.id
    assert alert.dedup_key == f"COMPLETION_REFUSED:{seed.stop.id}:2026-10-30"
    assert alert.description == (
        f"{seed.worker.full_name} tentou concluir Jardim (Praça central) a 120 m do local "
        "(limite 50 m)."
    )
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert notification.user_id == manager.id
    assert notification.kind == "ALERT"
    assert notification.title == "Conclusão recusada"
    assert notification.body == alert.description
    assert notification.data == {"alert_id": str(alert.id), "route_id": str(seed.route.id)}


async def test_without_a_position_the_wording_says_no_active_location(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)

    alert = await _refuse(db_session, seed, reason="LOCATION_REQUIRED", distance_m=None)

    assert alert is not None
    assert alert.description == (
        f"{seed.worker.full_name} tentou concluir Jardim (Praça central) sem localização ativa."
    )


async def test_a_second_refusal_the_same_day_adds_nothing(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _manager(db_session)
    first = await _refuse(db_session, seed)

    second = await _refuse(db_session, seed, distance_m=300.0)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 1


async def test_a_resolved_alert_is_not_reopened_the_same_day(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _manager(db_session)
    first = await _refuse(db_session, seed)
    assert first is not None
    first.status = AlertStatus.RESOLVED
    await db_session.flush()

    second = await _refuse(db_session, seed)

    assert second is not None
    assert second.status is AlertStatus.RESOLVED
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 1


async def test_the_next_day_raises_a_new_alert(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _manager(db_session)
    await _refuse(db_session, seed)

    await _refuse(db_session, seed, campus_day=date(2026, 10, 31))

    assert await _count(db_session, Alert) == 2
    assert await _count(db_session, Notification) == 2


async def test_a_disabled_rule_records_nothing(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _manager(db_session)
    await db_session.merge(AlertRule(rule_kind=AlertKind.COMPLETION_REFUSED, enabled=False))
    await db_session.flush()

    alert = await _refuse(db_session, seed)

    assert alert is None
    assert await _count(db_session, Alert) == 0
    assert await _count(db_session, Notification) == 0
