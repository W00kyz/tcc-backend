"""The release scan (spec 2026-10-20-alocacao-automatica §7.2-§7.4): every minute, before the
alert scan, give back to the pool the batch of a worker who left the campus or went silent, and
at the end of the workday close every open route and raise one "pontos não atendidos" alert.

Transaction discipline: like `run_alert_scan`, this driver COMMITS — the scheduler runs it with
its own short-lived session — and then notifies the managers best-effort.

    new_alerts = await run_release_scan(db, now=datetime.now(UTC))
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.campus_clock import campus_today
from app.domain.catalog.models import ContractorCompany, ServicePoint, ServiceType
from app.domain.monitoring.alerts_service import alert_rule_enabled
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity, WorkerPosition
from app.domain.monitoring.push import LoggingPushSender, PushSender
from app.domain.monitoring.rules import AlertDraft
from app.domain.monitoring.scan import insert_new_alerts, notify_new_alerts
from app.domain.reports.point_days import served_on
from app.domain.routing.models import ReleaseReason, Route, RouteStatus, RouteStop
from app.domain.routing.release import (
    LIVE_STOP_STATUSES,
    last_activity_at,
    last_scan_at,
    release_route,
)
from app.domain.settings.campus import (
    inside_campus,
    inside_campus_clause,
    is_service_day,
    workday_over,
)
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row


async def run_release_scan(
    db: AsyncSession, *, now: datetime, push: PushSender | None = None
) -> list[Alert]:
    """Release what nobody is serving any more, COMMIT, then notify; returns the alerts this pass
    inserted. Routes of a past day still open are closed first, whatever the day; besides
    that, nothing happens on a day outside `service_weekdays` (spec §7.3.1). Idempotent: a
    released route is no longer IN_PROGRESS and the day alert is deduplicated per day."""
    settings = await get_settings_row(db)
    today = campus_today(now)
    await _close_past_days(db, today=today, now=now)
    if not is_service_day(settings, today):
        await db.commit()
        return []

    drafts: list[AlertDraft] = []
    for route in await _lock_working_routes(db, today):
        draft = await _release_if_gone(db, route=route, settings=settings, now=now)
        if draft is not None:
            drafts.append(draft)
    if workday_over(settings, now):
        drafts.extend(await _close_the_day(db, today=today, now=now))

    inserted = await insert_new_alerts(
        db, [d for d in drafts if await alert_rule_enabled(db, d.kind)]
    )
    await db.commit()
    await notify_new_alerts(db, inserted, push or LoggingPushSender())
    return inserted


# --- left campus / location silence ----------------------------------------------------------


async def _lock_working_routes(db: AsyncSession, today: date) -> Sequence[Route]:
    """Today's IN_PROGRESS routes still holding a live stop, row-locked until the commit.

    SKIP LOCKED: a route locked right now is being handed a new batch (the allocation locks it
    too); the decision taken here would be stale, so it waits for the next pass instead of
    blocking the worker's request."""
    live_stop = exists().where(
        RouteStop.route_id == Route.id, RouteStop.status.in_(LIVE_STOP_STATUSES)
    )
    stmt = (
        select(Route)
        .where(Route.route_date == today, Route.status == RouteStatus.IN_PROGRESS, live_stop)
        .options(selectinload(Route.field_worker))
        .with_for_update(of=Route, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return (await db.scalars(stmt)).all()


async def _release_if_gone(
    db: AsyncSession, *, route: Route, settings: SystemSettings, now: datetime
) -> AlertDraft | None:
    """Release `route` (→ PAUSED) when its worker left the campus or went silent, and draft the
    BATCH_RELEASED alert naming why; None when the worker is still around."""
    if await _left_campus(db, route=route, settings=settings, now=now):
        reason, why = ReleaseReason.LEFT_CAMPUS, "fora do campus"
        minutes = settings.outside_campus_release_minutes
    elif await last_activity_at(db, route=route) < now - timedelta(
        minutes=settings.location_silence_release_minutes
    ):
        reason, why = ReleaseReason.LOCATION_SILENCE, "sem enviar localização"
        minutes = settings.location_silence_release_minutes
    else:
        return None
    released = await release_route(db, route=route, reason=reason, at=now, actor_id=None)
    return AlertDraft(
        kind=AlertKind.BATCH_RELEASED,
        severity=AlertSeverity.WARNING,
        dedup_key=f"BATCH_RELEASED:{route.id}:{now:%Y%m%dT%H%M}",
        description=(
            f"Lote de {route.field_worker.full_name} liberado: {len(released)} ponto(s) de volta "
            f"ao pool, {why} há mais de {minutes} min."
        ),
        route_id=route.id,
        field_worker_id=route.field_worker_id,
    )


async def _left_campus(
    db: AsyncSession, *, route: Route, settings: SystemSettings, now: datetime
) -> bool:
    """The worker has been outside the campus for `outside_campus_release_minutes` (spec §7.2):
    the latest position is outside, it is newer than the batch — a fix from before the worker
    came back and got this batch says nothing about it — and the first outside fix since they
    were last seen inside is at least that old. Seen inside: an inside position, or any QR scan
    — every point is on campus, and a scan counts as a received position (§7.3)."""
    latest = await db.scalar(
        select(WorkerPosition)
        .where(WorkerPosition.route_id == route.id)
        .order_by(WorkerPosition.reported_at.desc())
        .limit(1)
    )
    if latest is None or inside_campus(settings, latest.latitude, latest.longitude):
        return False
    if route.last_allocated_at is not None and latest.reported_at <= route.last_allocated_at:
        return False
    outside_since = await _first_outside_fix_since_seen_inside(db, route=route, settings=settings)
    limit = now - timedelta(minutes=settings.outside_campus_release_minutes)
    return outside_since is not None and outside_since <= limit


async def _first_outside_fix_since_seen_inside(
    db: AsyncSession, *, route: Route, settings: SystemSettings
) -> datetime | None:
    """When the current stretch outside the campus began: the earliest outside position after
    the worker was last seen inside. None when there is no such position."""
    inside = inside_campus_clause(settings, WorkerPosition.latitude, WorkerPosition.longitude)
    last_inside_position = await db.scalar(
        select(func.max(WorkerPosition.reported_at)).where(
            WorkerPosition.route_id == route.id, inside
        )
    )
    last_scan = await last_scan_at(db, route=route)
    seen_inside = [moment for moment in (last_inside_position, last_scan) if moment is not None]
    stmt = select(func.min(WorkerPosition.reported_at)).where(
        WorkerPosition.route_id == route.id, ~inside
    )
    if seen_inside:
        stmt = stmt.where(WorkerPosition.reported_at > max(seen_inside))
    first_outside: datetime | None = await db.scalar(stmt)
    return first_outside


# --- end of the workday ----------------------------------------------------------------------


async def _close_the_day(db: AsyncSession, *, today: date, now: datetime) -> list[AlertDraft]:
    """Close every open route of `today` (live stops → RELEASED `END_OF_DAY`) and draft the one
    POINTS_UNSERVED alert of the day when some typed point was not served (spec §7.4)."""
    open_routes = await db.scalars(
        select(Route)
        .where(
            Route.route_date == today,
            Route.status.in_([RouteStatus.IN_PROGRESS, RouteStatus.PAUSED]),
        )
        .with_for_update(skip_locked=True)
    )
    for route in open_routes.all():
        await release_route(
            db, route=route, reason=ReleaseReason.END_OF_DAY, at=now, actor_id=None, close=True
        )

    unserved = await _unserved_per_type(db, today)
    if not unserved:
        return []
    total = sum(count for _, _, count in unserved)
    per_type = "; ".join(f"{kind} ({company}): {count}" for kind, company, count in unserved)
    return [
        AlertDraft(
            kind=AlertKind.POINTS_UNSERVED,
            severity=AlertSeverity.WARNING,
            dedup_key=f"POINTS_UNSERVED:{today}",
            description=(
                f"Expediente encerrado com {total} ponto(s) não atendido(s) hoje: {per_type}."
            ),
            route_id=None,
        )
    ]


async def _close_past_days(db: AsyncSession, *, today: date, now: datetime) -> None:
    """Close the routes of a past day still open (live stops → RELEASED `END_OF_DAY`): the
    scheduler was down over that day's end, so `_close_the_day` never ran for them, and the
    allocation no longer serves them. A Friday missed is closed on Saturday too. No
    POINTS_UNSERVED alert for that day: it is past, the managers see it in the reports."""
    stale = await db.scalars(
        select(Route)
        .where(
            Route.route_date < today,
            Route.status.in_([RouteStatus.IN_PROGRESS, RouteStatus.PAUSED]),
        )
        .with_for_update(skip_locked=True)
    )
    for route in stale.all():
        await release_route(
            db, route=route, reason=ReleaseReason.END_OF_DAY, at=now, actor_id=None, close=True
        )


async def _unserved_per_type(db: AsyncSession, day: date) -> list[tuple[str, str, int]]:
    """(service type, company, points not served on `day`) for every type with any, by type
    name. Only typed points of a company count — an untyped point is in nobody's pool."""
    rows = await db.execute(
        select(ServiceType.name, ContractorCompany.name, func.count(ServicePoint.id))
        .join(ServiceType, ServicePoint.service_type_id == ServiceType.id)
        .join(ContractorCompany, ServiceType.contractor_company_id == ContractorCompany.id)
        .where(~served_on(day))
        .group_by(ServiceType.name, ContractorCompany.name)
        .order_by(ServiceType.name)
    )
    return [(kind, company, count) for kind, company, count in rows.tuples()]
