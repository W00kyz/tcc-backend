"""The monitoring board read model (RF41, spec §2 Ruling 9; spec 2026-10-20-alocacao-automatica
§8.2). `build_board` turns the current route / stop / alert / GPS state into one `BoardSnapshot`
— the payload `GET /monitoring/board` returns and the SSE stream diffs and re-emits on change.

The board only follows the day, it plans nothing: per service type, how many of today's points
are done, in somebody's batch, or still free; who is working (each route with its current
batch) and who has not started yet; how many points went back to the pool today.

`open_alerts` lists both `OPEN` and `ACKNOWLEDGED` rows so the dashboard can show "a manager is
on it" distinctly; `open_alert_count` counts only `OPEN`. The derived `is_delayed` flag went with
the planned start times and arrival windows (§8.7).

    snapshot = await build_board(db, now=datetime.now(UTC))
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel
from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.campus_clock import CAMPUS_TIMEZONE, campus_today
from app.domain.catalog.models import (
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    ServicePoint,
    ServiceType,
)
from app.domain.monitoring.models import Alert, AlertKind, AlertStatus, WorkerPosition
from app.domain.reports.point_days import served_on
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from app.domain.routing.release import LIVE_STOP_STATUSES
from app.domain.settings.campus import is_service_day
from app.domain.settings.service import get_settings_row

# Same scope as the alert scan: a stale route of an earlier day still open shows on the board
# (spec §2 Ruling 1). Today's routes show whatever their status.
_OPEN_ROUTE_STATUSES = (RouteStatus.IN_PROGRESS, RouteStatus.PAUSED)
# Statuses the dashboard still treats as "open work" — RESOLVED drops off the board.
_BOARD_ALERT_STATUSES = (AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED)


class BoardStop(BaseModel):
    id: uuid.UUID
    service_point_id: uuid.UUID
    service_point_name: str
    status: RouteStopStatus


class BoardRoute(BaseModel):
    route_id: uuid.UUID
    field_worker_id: uuid.UUID
    field_worker_name: str
    company_name: str
    route_date: date
    status: str
    open_alert_count: int
    # The route's highest batch number; 0 before any batch was handed out.
    current_batch: int
    # The current batch's stops still to do (PENDING / IN_PROGRESS), in route order.
    stops: list[BoardStop]
    done_today: int


class PoolKpi(BaseModel):
    """Today's typed points of one service type: `done` (served, also by appeal) + `in_progress`
    (in somebody's batch) + `free` (in the pool) = `total`."""

    service_type_id: uuid.UUID
    service_type_name: str
    company_name: str
    total: int
    done: int
    in_progress: int
    free: int


class WorkerRef(BaseModel):
    id: uuid.UUID
    full_name: str
    company_name: str


class BoardAlert(BaseModel):
    id: uuid.UUID
    kind: AlertKind
    severity: str
    description: str
    route_id: uuid.UUID | None
    route_stop_id: uuid.UUID | None
    field_worker_id: uuid.UUID | None
    status: AlertStatus
    created_at: datetime


class BoardPosition(BaseModel):
    field_worker_id: uuid.UUID
    field_worker_name: str
    latitude: float
    longitude: float
    reported_at: datetime
    age_seconds: int


class BoardSnapshot(BaseModel):
    generated_at: datetime
    # False on a day outside `service_weekdays`: no pool, nobody expected to start.
    service_day: bool
    routes: list[BoardRoute]
    pool: list[PoolKpi]
    # Approved, active workers with no route today — informative only.
    not_started: list[WorkerRef]
    # Stops given back to the pool today, whatever the reason.
    released_today: int
    open_alerts: list[BoardAlert]
    worker_positions: list[BoardPosition]


async def build_board(db: AsyncSession, *, now: datetime) -> BoardSnapshot:
    """Load today's routes, the pool, the board alerts and the latest GPS fix per worker — a
    fixed number of queries, no N+1 — and fold them into a `BoardSnapshot`."""
    today = campus_today(now)
    service_day = is_service_day(await get_settings_row(db), today)
    routes = await _load_routes(db, today)
    alerts = await _load_board_alerts(db, [route.id for route in routes], today)
    open_by_route: dict[uuid.UUID | None, int] = defaultdict(int)
    for alert in alerts:
        if alert.status is AlertStatus.OPEN:
            open_by_route[alert.route_id] += 1

    workers = await _worker_refs(db, [route.field_worker_id for route in routes])
    positions = await _latest_positions(db, list(workers))
    return BoardSnapshot(
        generated_at=now,
        service_day=service_day,
        routes=[
            _to_board_route(route, workers[route.field_worker_id], open_by_route[route.id])
            for route in routes
        ],
        pool=await _pool_kpis(db, today) if service_day else [],
        not_started=await _not_started(db, today) if service_day else [],
        released_today=await _released_count(db, today),
        open_alerts=[_to_board_alert(alert) for alert in alerts],
        worker_positions=[
            _to_board_position(position, workers[position.field_worker_id].full_name, now)
            for position in positions
        ],
    )


async def _load_routes(db: AsyncSession, today: date) -> Sequence[Route]:
    stale_open = and_(Route.route_date < today, Route.status.in_(_OPEN_ROUTE_STATUSES))
    stmt = (
        select(Route)
        .where(or_(Route.route_date == today, stale_open))
        .options(selectinload(Route.stops).selectinload(RouteStop.service_point))
        .order_by(Route.route_date, Route.created_at)
    )
    return (await db.scalars(stmt)).all()


async def _load_board_alerts(
    db: AsyncSession, route_ids: Sequence[uuid.UUID], today: date
) -> Sequence[Alert]:
    """The open alerts of the board's routes plus today's route-less ones (a day-level alert
    such as POINTS_UNSERVED has no route to hang from)."""
    day_start = datetime.combine(today, time.min, tzinfo=CAMPUS_TIMEZONE)
    route_less_today = and_(
        Alert.route_id.is_(None),
        Alert.created_at >= day_start,
        Alert.created_at < day_start + timedelta(days=1),
    )
    stmt = (
        select(Alert)
        .where(
            Alert.status.in_(_BOARD_ALERT_STATUSES),
            or_(Alert.route_id.in_(route_ids), route_less_today),
        )
        .order_by(Alert.created_at)
    )
    return (await db.scalars(stmt)).all()


def _worker_ref_query() -> Select[tuple[uuid.UUID, str, str]]:
    return select(FieldWorker.id, FieldWorker.full_name, ContractorCompany.name).join(
        ContractorCompany, FieldWorker.contractor_company_id == ContractorCompany.id
    )


async def _worker_refs(
    db: AsyncSession, worker_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, WorkerRef]:
    if not worker_ids:
        return {}
    rows = await db.execute(_worker_ref_query().where(FieldWorker.id.in_(worker_ids)))
    return {
        worker_id: WorkerRef(id=worker_id, full_name=name, company_name=company)
        for worker_id, name, company in rows.tuples()
    }


async def _not_started(db: AsyncSession, today: date) -> list[WorkerRef]:
    has_route_today = exists().where(
        Route.field_worker_id == FieldWorker.id, Route.route_date == today
    )
    stmt = (
        _worker_ref_query()
        .where(
            FieldWorker.status == FieldWorkerStatus.APPROVED,
            FieldWorker.is_active.is_(True),
            ~has_route_today,
        )
        .order_by(ContractorCompany.name, FieldWorker.full_name)
    )
    return [
        WorkerRef(id=worker_id, full_name=name, company_name=company)
        for worker_id, name, company in (await db.execute(stmt)).tuples()
    ]


async def _pool_kpis(db: AsyncSession, today: date) -> list[PoolKpi]:
    """Today's typed points per service type. A served point counts as done even when it is
    also back in somebody's batch; so done, in progress and free never overlap."""
    in_a_batch = exists().where(
        RouteStop.service_point_id == ServicePoint.id,
        RouteStop.service_date == today,
        RouteStop.status.in_(LIVE_STOP_STATUSES),
    )
    points = select(
        ServicePoint.service_type_id,
        served_on(today).label("served"),
        in_a_batch.label("in_a_batch"),
    ).subquery()
    stmt = (
        select(
            ServiceType.id,
            ServiceType.name,
            ContractorCompany.name,
            func.count(),
            func.count().filter(points.c.served),
            func.count().filter(~points.c.served, points.c.in_a_batch),
        )
        .select_from(points)
        .join(ServiceType, points.c.service_type_id == ServiceType.id)
        .join(ContractorCompany, ServiceType.contractor_company_id == ContractorCompany.id)
        .group_by(ServiceType.id, ServiceType.name, ContractorCompany.name)
        .order_by(ContractorCompany.name, ServiceType.name)
    )
    return [
        PoolKpi(
            service_type_id=type_id,
            service_type_name=type_name,
            company_name=company,
            total=total,
            done=done,
            in_progress=in_progress,
            free=total - done - in_progress,
        )
        for type_id, type_name, company, total, done, in_progress in (
            await db.execute(stmt)
        ).tuples()
    ]


async def _released_count(db: AsyncSession, today: date) -> int:
    stmt = select(func.count(RouteStop.id)).where(
        RouteStop.service_date == today, RouteStop.status == RouteStopStatus.RELEASED
    )
    return (await db.execute(stmt)).scalar_one()


async def _latest_positions(
    db: AsyncSession, worker_ids: Sequence[uuid.UUID]
) -> Sequence[WorkerPosition]:
    """The newest `worker_positions` row per worker in one query. `DISTINCT ON (field_worker_id)`
    with a matching leading ORDER BY keeps the first row of each worker group — the latest fix."""
    if not worker_ids:
        return []
    stmt = (
        select(WorkerPosition)
        .where(WorkerPosition.field_worker_id.in_(worker_ids))
        .distinct(WorkerPosition.field_worker_id)
        .order_by(WorkerPosition.field_worker_id, WorkerPosition.reported_at.desc())
    )
    return (await db.scalars(stmt)).all()


def _to_board_route(route: Route, worker: WorkerRef, open_alert_count: int) -> BoardRoute:
    current_batch = max((stop.batch_number for stop in route.stops), default=0)
    return BoardRoute(
        route_id=route.id,
        field_worker_id=route.field_worker_id,
        field_worker_name=worker.full_name,
        company_name=worker.company_name,
        route_date=route.route_date,
        status=route.status.value,
        open_alert_count=open_alert_count,
        current_batch=current_batch,
        stops=[
            BoardStop(
                id=stop.id,
                service_point_id=stop.service_point_id,
                service_point_name=stop.service_point.name,
                status=stop.status,
            )
            for stop in route.stops
            if stop.batch_number == current_batch and stop.status in LIVE_STOP_STATUSES
        ],
        done_today=sum(1 for stop in route.stops if stop.status is RouteStopStatus.DONE),
    )


def _to_board_alert(alert: Alert) -> BoardAlert:
    return BoardAlert(
        id=alert.id,
        kind=alert.kind,
        severity=alert.severity.value,
        description=alert.description,
        route_id=alert.route_id,
        route_stop_id=alert.route_stop_id,
        field_worker_id=alert.field_worker_id,
        status=alert.status,
        created_at=alert.created_at,
    )


def _to_board_position(position: WorkerPosition, name: str, now: datetime) -> BoardPosition:
    age = int((now - position.reported_at).total_seconds())
    return BoardPosition(
        field_worker_id=position.field_worker_id,
        field_worker_name=name,
        latitude=position.latitude,
        longitude=position.longitude,
        reported_at=position.reported_at,
        age_seconds=max(0, age),
    )
