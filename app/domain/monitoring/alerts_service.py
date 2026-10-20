"""Etapa 9 — the alert handling lifecycle (spec §2 Ruling 7) and the configurable alert
rules (Ruling 8).

Transaction discipline: every function here flushes, never commits. The endpoint on
`app/api/monitoring.py` owns the transaction boundary and appends the audit-trail entry.

    alert = await acknowledge_alert(db, alert=alert, actor_id=actor.id)
    await db.commit()
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.monitoring.models import Alert, AlertKind, AlertRule, AlertStatus


class AlertError(Exception):
    """Base class for the alert-lifecycle signal conditions the router catches by name."""


class AlertAlreadyResolved(AlertError):
    """The alert is RESOLVED and cannot be acknowledged again — mapped to 409."""


async def list_alerts(
    db: AsyncSession,
    *,
    status: AlertStatus | None = None,
    route_id: uuid.UUID | None = None,
) -> Sequence[Alert]:
    """The manager dashboard's alert list, newest first. Both filters are optional and AND
    together when both are given."""
    stmt = select(Alert).order_by(Alert.created_at.desc())
    if status is not None:
        stmt = stmt.where(Alert.status == status)
    if route_id is not None:
        stmt = stmt.where(Alert.route_id == route_id)
    return (await db.scalars(stmt)).all()


async def acknowledge_alert(db: AsyncSession, *, alert: Alert, actor_id: uuid.UUID) -> Alert:
    """Move an OPEN alert to ACKNOWLEDGED and stamp the manager (Cenário 5 step 7).

    Idempotent for an already-ACKNOWLEDGED alert — the original `acknowledged_by`/`_at` stand,
    no re-stamp. A RESOLVED alert raises `AlertAlreadyResolved`: the handling cycle is closed."""
    if alert.status is AlertStatus.RESOLVED:
        raise AlertAlreadyResolved(
            f'Alert "{alert.id}" is RESOLVED and cannot be acknowledged; expected OPEN or '
            "ACKNOWLEDGED."
        )
    if alert.status is AlertStatus.ACKNOWLEDGED:
        return alert
    alert.status = AlertStatus.ACKNOWLEDGED
    alert.acknowledged_by = actor_id
    alert.acknowledged_at = datetime.now(UTC)
    await db.flush()
    return alert


async def resolve_alert(db: AsyncSession, *, alert: Alert, actor_id: uuid.UUID) -> Alert:
    """Close an alert (Cenário 5 step 8) from OPEN or ACKNOWLEDGED. Idempotent for an
    already-RESOLVED alert — the original `resolved_by`/`_at` stand."""
    if alert.status is AlertStatus.RESOLVED:
        return alert
    alert.status = AlertStatus.RESOLVED
    alert.resolved_by = actor_id
    alert.resolved_at = datetime.now(UTC)
    await db.flush()
    return alert


async def _ensure_rules(db: AsyncSession) -> None:
    """Recreate any missing `alert_rules` row with its seed default. The migration seeds all
    four, but tests truncate every table between cases — same defence as `get_settings_row`."""
    existing = {rule.rule_kind for rule in await db.scalars(select(AlertRule))}
    missing = [kind for kind in AlertKind if kind not in existing]
    for kind in missing:
        db.add(AlertRule(rule_kind=kind, enabled=True, threshold_minutes=None))
    if missing:
        await db.flush()


async def list_alert_rules(db: AsyncSession) -> Sequence[AlertRule]:
    """The four rule rows, ordered by kind — the manager's rule-config screen (RF50)."""
    await _ensure_rules(db)
    stmt = select(AlertRule).order_by(AlertRule.rule_kind)
    return (await db.scalars(stmt)).all()


async def get_alert_rule(db: AsyncSession, rule_kind: AlertKind) -> AlertRule | None:
    await _ensure_rules(db)
    return await db.get(AlertRule, rule_kind)


async def update_alert_rule(
    db: AsyncSession,
    *,
    rule: AlertRule,
    enabled: bool | None,
    threshold_minutes: int | None,
    actor_id: uuid.UUID,
    clear_threshold: bool = False,
) -> AlertRule:
    """Patch semantics — a `None` means "leave unchanged". `clear_threshold=True` drops the
    override back to NULL, i.e. the global default from system_settings (Ruling 8).

    The dashboard's "Regras de alerta" offers exactly that — an emptied threshold input means
    "usar o padrão global" — and used to be silently ignored here (it sent `null`, which read
    as "unchanged"), so the override could never be removed.

        await update_alert_rule(db, rule=r, enabled=None, threshold_minutes=None,
                                actor_id=uid, clear_threshold=True)
    """
    if enabled is not None:
        rule.enabled = enabled
    if clear_threshold:
        rule.threshold_minutes = None
    elif threshold_minutes is not None:
        rule.threshold_minutes = threshold_minutes
    rule.updated_by = actor_id
    rule.updated_at = datetime.now(UTC)
    await db.flush()
    return rule
