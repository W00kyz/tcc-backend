"""The monitoring board read model (RF41, spec §2 Ruling 9). `build_board` turns the current
route / stop / alert / GPS state into one `BoardSnapshot` — the payload `GET /monitoring/board`
returns and the SSE stream (Task 8) diffs and re-emits on change.

`open_alerts` lists both `OPEN` and `ACKNOWLEDGED` rows so the dashboard can show "a manager is
on it" distinctly; `open_alert_count` counts only `OPEN`. The derived `is_delayed` flag went with
the planned start times and arrival windows (spec 2026-10-20-alocacao-automatica §8.7).

    snapshot = await build_board(db, now=datetime.now(UTC))
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.campus_clock import campus_today
from app.domain.catalog.models import FieldWorker
from app.domain.monitoring.models import Alert, AlertKind, AlertStatus, WorkerPosition
from app.domain.routing.models import Route, RouteStatus, RouteStopStatus

# Same scope as the alert scan: a stale route still open shows on the board (spec §2 Ruling 1).
_BOARD_STATUSES = (RouteStatus.IN_PROGRESS, RouteStatus.PAUSED)
# Statuses the dashboard still treats as "open work" — RESOLVED drops off the board.
_BOARD_ALERT_STATUSES = (AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED)


class BoardRoute(BaseModel):
    route_id: uuid.UUID
    field_worker_id: uuid.UUID
    field_worker_name: str
    route_date: date
    status: str
    open_alert_count: int
    stop_total: int
    stop_done: int


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
    routes: list[BoardRoute]
    open_alerts: list[BoardAlert]
    worker_positions: list[BoardPosition]


async def build_board(db: AsyncSession, *, now: datetime) -> BoardSnapshot:
    """Load routes+stops, board alerts and the latest GPS fix per worker — four queries, no
    N+1 — and fold them into a `BoardSnapshot`."""
    routes = await _load_routes(db, now)
    alerts = await _load_board_alerts(db, [route.id for route in routes])
    open_by_route: dict[uuid.UUID | None, int] = defaultdict(int)
    for alert in alerts:
        if alert.status is AlertStatus.OPEN:
            open_by_route[alert.route_id] += 1

    worker_ids = [route.field_worker_id for route in routes]
    names = await _worker_names(db, worker_ids)
    positions = await _latest_positions(db, worker_ids)

    board_routes = [
        _to_board_route(
            route,
            name=names.get(route.field_worker_id, ""),
            open_alert_count=open_by_route[route.id],
        )
        for route in routes
    ]
    return BoardSnapshot(
        generated_at=now,
        routes=board_routes,
        open_alerts=[_to_board_alert(alert) for alert in alerts],
        worker_positions=[
            _to_board_position(position, names.get(position.field_worker_id, ""), now)
            for position in positions
        ],
    )


async def _load_routes(db: AsyncSession, now: datetime) -> Sequence[Route]:
    stmt = (
        select(Route)
        .where(Route.route_date <= campus_today(now), Route.status.in_(_BOARD_STATUSES))
        .options(selectinload(Route.stops))
        .order_by(Route.route_date, Route.created_at)
    )
    return (await db.scalars(stmt)).all()


async def _load_board_alerts(db: AsyncSession, route_ids: Sequence[uuid.UUID]) -> Sequence[Alert]:
    if not route_ids:
        return []
    stmt = (
        select(Alert)
        .where(Alert.route_id.in_(route_ids), Alert.status.in_(_BOARD_ALERT_STATUSES))
        .order_by(Alert.created_at)
    )
    return (await db.scalars(stmt)).all()


async def _worker_names(db: AsyncSession, worker_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not worker_ids:
        return {}
    rows = await db.execute(
        select(FieldWorker.id, FieldWorker.full_name).where(FieldWorker.id.in_(worker_ids))
    )
    return {row.id: row.full_name for row in rows}


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


def _to_board_route(route: Route, *, name: str, open_alert_count: int) -> BoardRoute:
    return BoardRoute(
        route_id=route.id,
        field_worker_id=route.field_worker_id,
        field_worker_name=name,
        route_date=route.route_date,
        status=route.status.value,
        open_alert_count=open_alert_count,
        stop_total=len(route.stops),
        stop_done=sum(1 for stop in route.stops if stop.status is RouteStopStatus.DONE),
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
