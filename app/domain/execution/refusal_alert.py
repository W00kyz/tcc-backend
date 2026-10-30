"""The COMPLETION_REFUSED alert (spec 2026-10-30-locais-externos §4): when a completion by
location is refused for lack of a position or for distance, the managers learn that a worker
tried to close a stop without being at the place.

One alert per stop and campus day (`dedup_key`): the worker retrying from the same spot does not
flood the board, and a resolved alert is not reopened by the same day's retries (the `alerts`
UNIQUE key semantics). Flush, never commit — the router commits, then answers the 422, so the
alert outlives the refusal.
"""

from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import FieldWorker, ServicePoint, ServiceType
from app.domain.catalog.sites import site_of
from app.domain.monitoring.alerts_service import alert_rule_enabled
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity
from app.domain.monitoring.notifications import notify_completion_refused
from app.domain.monitoring.push import PushSender
from app.domain.routing.models import RouteStop

RefusalReason = Literal["LOCATION_REQUIRED", "OUT_OF_RADIUS"]


async def record_completion_refusal(
    db: AsyncSession,
    *,
    stop: RouteStop,
    worker: FieldWorker,
    reason: RefusalReason,
    distance_m: float | None,
    radius_m: int,
    campus_day: date,
    push: PushSender,
) -> Alert | None:
    """Open the stop's COMPLETION_REFUSED alert for `campus_day` and notify the managers.

    Returns `None` when the rule is switched off, and the day's existing alert (open or
    resolved, no second notification) when the stop was already refused today.

        alert = await record_completion_refusal(db, stop=stop, worker=worker,
                                                reason="OUT_OF_RADIUS", distance_m=120.4,
                                                radius_m=50, campus_day=today, push=push)
    """
    if not await alert_rule_enabled(db, AlertKind.COMPLETION_REFUSED):
        return None
    dedup_key = f"COMPLETION_REFUSED:{stop.id}:{campus_day.isoformat()}"
    existing = await db.scalar(select(Alert).where(Alert.dedup_key == dedup_key))
    if existing is not None:
        return existing
    alert = Alert(
        kind=AlertKind.COMPLETION_REFUSED,
        severity=AlertSeverity.WARNING,
        dedup_key=dedup_key,
        description=await _describe(
            db, stop=stop, worker=worker, reason=reason, distance_m=distance_m, radius_m=radius_m
        ),
        route_id=stop.route_id,
        route_stop_id=stop.id,
        field_worker_id=worker.id,
    )
    db.add(alert)
    await db.flush()
    await notify_completion_refused(db, alert=alert, push=push)
    return alert


async def _describe(
    db: AsyncSession,
    *,
    stop: RouteStop,
    worker: FieldWorker,
    reason: RefusalReason,
    distance_m: float | None,
    radius_m: int,
) -> str:
    place = await _place_label(db, stop)
    if reason == "LOCATION_REQUIRED" or distance_m is None:
        return f"{worker.full_name} tentou concluir {place} sem localização ativa."
    return (
        f"{worker.full_name} tentou concluir {place} a {round(distance_m)} m do local "
        f"(limite {radius_m} m)."
    )


async def _place_label(db: AsyncSession, stop: RouteStop) -> str:
    """What was being done and where: "Jardinagem (Praça central)". A place's points are named
    after the place, so the first part is the stop's service type, not the point."""
    point = await db.scalar(
        select(ServicePoint)
        .where(ServicePoint.id == stop.service_point_id)
        .options(selectinload(ServicePoint.place))
    )
    assert point is not None  # the foreign key guarantees it
    service_type = await db.get(ServiceType, stop.service_type_id) if stop.service_type_id else None
    what = service_type.name if service_type is not None else point.name
    if point.place is None:
        return what
    return f"{what} ({site_of(point).name})"
