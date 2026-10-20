"""Notification helpers (spec §2 Ruling 3). `notifications` is the canonical delivery record:
every helper persists the rows FIRST (flush, never commit — the caller owns the transaction),
THEN fans out to the `PushSender` seam best-effort. A push failure is swallowed and logged; it
never raises out of these helpers and never touches the already-flushed rows.

    rows = await notify_alert(db, alert=alert, push=push)
    await db.commit()
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, Notification
from app.domain.monitoring.push import PushSender
from app.domain.routing.models import Route, RouteStop

logger = logging.getLogger(__name__)

# pt-BR title per alert kind — the notification body carries the full description.
_ALERT_TITLE_PT_BR: dict[AlertKind, str] = {
    AlertKind.NO_EVIDENCE: "Execução sem evidência",
    AlertKind.POINT_IMPEDED: "Ponto impedido",
    AlertKind.BATCH_RELEASED: "Pontos liberados",
    AlertKind.POINTS_UNSERVED: "Pontos não atendidos",
    AlertKind.APPEAL_FILED: "Novo recurso",
}


async def notify_users(
    db: AsyncSession,
    *,
    user_ids: list[uuid.UUID],
    kind: str,
    title: str,
    body: str,
    data: dict[str, object],
    push: PushSender,
) -> list[Notification]:
    """Persist one `Notification` per (deduplicated) user id, flush, then best-effort push.

    Flush, never commit — the caller owns the transaction. A `PushSender.send` that raises is
    logged and swallowed: the flushed rows stand regardless."""
    unique_ids = list(dict.fromkeys(user_ids))
    notifications = [
        Notification(user_id=user_id, kind=kind, title=title, body=body, data=data)
        for user_id in unique_ids
    ]
    db.add_all(notifications)
    await db.flush()

    for notification in notifications:
        try:
            await push.send(user_id=notification.user_id, title=title, body=body, data=data)
        except Exception:
            logger.exception(
                "push notification failed",
                extra={"event": "push_failed", "user_id": str(notification.user_id)},
            )
    return notifications


# pt-BR label per impediment reason (RF16) — the keys mirror `ImpedimentReason`'s values; a
# string map keeps this module free of an import on the impediments domain.
_IMPEDIMENT_REASON_PT_BR: dict[str, str] = {
    "ACCESS_BLOCKED": "acesso bloqueado",
    "SITE_REFUSAL": "recusa do local",
    "HEALTH_ISSUE": "problema de saúde",
    "OTHER": "outro motivo",
}


async def _manager_ids(db: AsyncSession) -> list[uuid.UUID]:
    return list(
        (
            await db.scalars(
                select(User.id).where(
                    User.role.in_([UserRole.MANAGER, UserRole.ADMIN]), User.is_active.is_(True)
                )
            )
        ).all()
    )


async def describe_impediment(
    db: AsyncSession,
    *,
    route: Route,
    route_stop: RouteStop | None,
    reason: str,
    released_count: int,
) -> str:
    """The pt-BR sentence naming who cannot go on, which point(s) went back to the pool and
    why — the body of the impediment notification and the POINT_IMPEDED alert alike.

        text = await describe_impediment(db, route=route, route_stop=None,
                                         reason="HEALTH_ISSUE", released_count=3)
    """
    field_worker = await db.get(FieldWorker, route.field_worker_id)
    worker_name = field_worker.full_name if field_worker is not None else "Profissional"
    reason_text = _IMPEDIMENT_REASON_PT_BR.get(reason, reason.lower())
    if route_stop is not None:
        point = await db.get(ServicePoint, route_stop.service_point_id)
        affected = f"o ponto {point.name if point is not None else ''}".rstrip()
    else:
        affected = f"o restante da rota ({released_count} ponto(s) de volta ao pool)"
    return f"{worker_name} não pode executar {affected}: {reason_text}."


async def notify_impediment_reported(
    db: AsyncSession,
    *,
    impediment_id: uuid.UUID,
    route: Route,
    description: str,
    push: PushSender,
) -> list[Notification]:
    """Tell every manager that a field worker cannot go on (RF17); `description` comes from
    `describe_impediment`. Same audience as `notify_alert`: a route has no single owning
    manager (spec §2 Ruling 6). Flush, never commit.

        await notify_impediment_reported(db, impediment_id=imp.id, route=route,
                                         description=text, push=push)
    """
    return await notify_users(
        db,
        user_ids=await _manager_ids(db),
        kind="IMPEDIMENT_REPORTED",
        title="Impossibilidade sinalizada",
        body=description,
        data={"impediment_id": str(impediment_id), "route_id": str(route.id)},
        push=push,
    )


async def notify_alert(db: AsyncSession, *, alert: Alert, push: PushSender) -> list[Notification]:
    """Notify every MANAGER and ADMIN user of a freshly created alert (spec §2 Ruling 6 — a
    route has no single owning manager, so all of them see operational alerts)."""
    recipient_ids = list(
        (
            await db.scalars(
                select(User.id).where(User.role.in_([UserRole.MANAGER, UserRole.ADMIN]))
            )
        ).all()
    )
    title = _ALERT_TITLE_PT_BR.get(alert.kind, "Alerta")
    return await notify_users(
        db,
        user_ids=recipient_ids,
        kind="ALERT",
        title=title,
        body=alert.description,
        data=_alert_data(alert),
        push=push,
    )


def _alert_data(alert: Alert) -> dict[str, object]:
    # A day-level alert (POINTS_UNSERVED) belongs to no route.
    if alert.route_id is None:
        return {"alert_id": str(alert.id)}
    return {"alert_id": str(alert.id), "route_id": str(alert.route_id)}
