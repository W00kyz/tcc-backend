"""RF47-RF52 alert scan driver (spec §2 Ruling 1/2). `run_alert_scan` is the only side-effecting
piece of the monitoring rules: it loads route/stop/execution state once, hands hand-built
snapshots to the 4 pure rules (`app.domain.monitoring.rules`), and writes any new violations as
`Alert` rows with a single conflict-safe batch insert keyed by `dedup_key`.

Transaction discipline: unlike a domain service, this driver COMMITS. It is invoked by the
APScheduler job (Ruling 1) with its own short-lived session, not from inside an HTTP request that
owns a wider transaction — the deliberate exception, like `execution/service.py:check_out`.

Example:
    new_alerts = await run_alert_scan(db)  # -> list[Alert] actually inserted this pass
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.execution.models import EvidenceItem, Execution, ManualCompletion
from app.domain.monitoring.models import (
    Alert,
    AlertKind,
    AlertRule,
    AlertSeverity,
    AlertStatus,
)
from app.domain.monitoring.notifications import notify_alert
from app.domain.monitoring.push import LoggingPushSender, PushSender
from app.domain.monitoring.rules import (
    AlertDraft,
    AlertThresholds,
    RouteSnapshot,
    StopSnapshot,
    rule_late_start,
    rule_late_stop,
    rule_no_evidence,
    rule_worker_absent,
)
from app.domain.routing.models import Route, RouteStatus, RouteStop
from app.domain.settings.models import SystemSettings
from app.domain.settings.service import get_settings_row

logger = logging.getLogger(__name__)

_SCANNED_STATUSES = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)

# Below WARNING nobody is paged — an INFO alert is recorded but not fanned out (spec §2 Ruling 6).
_NOTIFIABLE_SEVERITIES = (AlertSeverity.WARNING, AlertSeverity.CRITICAL)


async def run_alert_scan(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    push: PushSender | None = None,
) -> list[Alert]:
    """Load current state, run the 4 alert rules, batch-insert new `Alert` rows, COMMIT.

    Returns only the rows this pass actually inserted (the `ON CONFLICT (dedup_key) DO NOTHING`
    swallows violations that already have an open/handled alert), so the caller can fan them out
    to `notify_alert`. Safe to run at any time and any number of times — idempotent by design.
    """
    now = now or datetime.now(UTC)

    routes = await _load_routes(db, now)
    settings = await get_settings_row(db)
    rules_by_kind = {r.rule_kind: r for r in await db.scalars(select(AlertRule))}

    snapshots = await _build_snapshots(db, routes)
    thresholds = _resolve_thresholds(settings, rules_by_kind)
    enabled = {kind: _is_enabled(rules_by_kind, kind) for kind in AlertKind}

    drafts: list[AlertDraft] = []
    for rule in (rule_late_start, rule_worker_absent, rule_late_stop, rule_no_evidence):
        drafts.extend(rule(snapshots, thresholds, now, enabled))

    inserted = await _insert_new_alerts(db, drafts)
    await db.commit()

    # The alerts are already committed above. Notifications are a separate best-effort batch:
    # a notify/push failure must never lose an alert, so it gets its own commit and a guard.
    await _notify_new_alerts(db, inserted, push or LoggingPushSender())
    return inserted


async def _notify_new_alerts(db: AsyncSession, inserted: list[Alert], push: PushSender) -> None:
    """Fan every WARNING/CRITICAL alert out to `notify_alert` (one `Notification` per
    MANAGER/ADMIN + best-effort push), then commit the notification rows. Any failure is
    logged and swallowed — the alerts are already committed and must survive it."""
    to_notify = [a for a in inserted if a.severity in _NOTIFIABLE_SEVERITIES]
    if not to_notify:
        return
    try:
        for alert in to_notify:
            await notify_alert(db, alert=alert, push=push)
        await db.commit()
    except Exception:
        logger.exception("alert notification failed", extra={"event": "alert_notify_failed"})
        await db.rollback()


async def _load_routes(db: AsyncSession, now: datetime) -> Sequence[Route]:
    """Today's (or an earlier day's still-open) routes with their stops eager-loaded. A route
    left IN_PROGRESS from a previous day must still raise LATE_STOP, hence `route_date <=`."""
    stmt = (
        select(Route)
        .where(Route.route_date <= now.date(), Route.status.in_(_SCANNED_STATUSES))
        .options(selectinload(Route.stops))
    )
    return (await db.scalars(stmt)).all()


async def _build_snapshots(db: AsyncSession, routes: Sequence[Route]) -> list[RouteSnapshot]:
    stop_ids = [stop.id for route in routes for stop in route.stops]
    latest_exec = await _latest_execution_per_stop(db, stop_ids)
    exec_ids = [e.id for e in latest_exec.values()]
    evidence_counts = await _evidence_counts(db, exec_ids)
    manual_stop_ids = await _stops_with_manual_completion(db, stop_ids)

    snapshots: list[RouteSnapshot] = []
    for route in routes:
        stops = [
            _stop_snapshot(stop, latest_exec, evidence_counts, manual_stop_ids)
            for stop in route.stops
        ]
        snapshots.append(
            RouteSnapshot(
                route_id=route.id,
                status=route.status.value,
                scheduled_start_at=route.scheduled_start_at,
                field_worker_id=route.field_worker_id,
                stops=stops,
            )
        )
    return snapshots


def _stop_snapshot(
    stop: RouteStop,
    latest_exec: dict[uuid.UUID, Execution],
    evidence_counts: dict[uuid.UUID, int],
    manual_stop_ids: set[uuid.UUID],
) -> StopSnapshot:
    execution = latest_exec.get(stop.id)
    return StopSnapshot(
        route_stop_id=stop.id,
        status=stop.status.value,
        expected_arrival_to=stop.expected_arrival_to,
        checked_out_at=execution.checked_out_at if execution is not None else None,
        execution_source=execution.source.value if execution is not None else None,
        evidence_count=evidence_counts.get(execution.id, 0) if execution is not None else 0,
        has_manual_completion=stop.id in manual_stop_ids,
    )


async def _latest_execution_per_stop(
    db: AsyncSession, stop_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Execution]:
    if not stop_ids:
        return {}
    rows = await db.scalars(
        select(Execution)
        .where(Execution.route_stop_id.in_(stop_ids))
        .order_by(Execution.checked_in_at.asc())
    )
    # asc order + last-write-wins leaves the newest execution per stop.
    return {execution.route_stop_id: execution for execution in rows}


async def _evidence_counts(
    db: AsyncSession, execution_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not execution_ids:
        return {}
    rows = await db.execute(
        select(EvidenceItem.execution_id, func.count(EvidenceItem.id))
        .where(EvidenceItem.execution_id.in_(execution_ids))
        .group_by(EvidenceItem.execution_id)
    )
    return dict(rows.tuples().all())


async def _stops_with_manual_completion(
    db: AsyncSession, stop_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    if not stop_ids:
        return set()
    rows = await db.scalars(
        select(ManualCompletion.route_stop_id).where(ManualCompletion.route_stop_id.in_(stop_ids))
    )
    return set(rows)


_GLOBAL_THRESHOLD_ATTR: dict[AlertKind, str] = {
    AlertKind.LATE_START: "late_start_tolerance_min",
    AlertKind.WORKER_ABSENT: "absence_tolerance_min",
    AlertKind.LATE_STOP: "late_stop_tolerance_min",
    AlertKind.NO_EVIDENCE: "no_evidence_grace_min",
}


def _resolve_thresholds(
    settings: SystemSettings, rules_by_kind: dict[AlertKind, AlertRule]
) -> AlertThresholds:
    """Per-kind override on `alert_rules.threshold_minutes` wins; NULL falls back to the matching
    global default on `system_settings` (Ruling 8)."""

    def minutes(kind: AlertKind) -> int:
        rule = rules_by_kind.get(kind)
        if rule is not None and rule.threshold_minutes is not None:
            return rule.threshold_minutes
        return int(getattr(settings, _GLOBAL_THRESHOLD_ATTR[kind]))

    return AlertThresholds(
        late_start_min=minutes(AlertKind.LATE_START),
        absence_min=minutes(AlertKind.WORKER_ABSENT),
        late_stop_min=minutes(AlertKind.LATE_STOP),
        no_evidence_min=minutes(AlertKind.NO_EVIDENCE),
    )


def _is_enabled(rules_by_kind: dict[AlertKind, AlertRule], kind: AlertKind) -> bool:
    rule = rules_by_kind.get(kind)
    return rule.enabled if rule is not None else True


async def _insert_new_alerts(db: AsyncSession, drafts: list[AlertDraft]) -> list[Alert]:
    if not drafts:
        return []
    values = [
        {
            "id": uuid.uuid4(),
            "kind": draft.kind.value,
            "severity": draft.severity.value,
            "dedup_key": draft.dedup_key,
            "description": draft.description,
            "route_id": draft.route_id,
            "route_stop_id": draft.route_stop_id,
            "field_worker_id": draft.field_worker_id,
            # A bulk insert().values() bypasses the model's Python-side `default=OPEN`; created_at
            # is a server_default so it takes care of itself.
            "status": AlertStatus.OPEN.value,
        }
        for draft in drafts
    ]
    stmt = (
        pg_insert(Alert)
        .values(values)
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Alert.id)
    )
    inserted_ids = list((await db.execute(stmt)).scalars())
    await db.flush()
    if not inserted_ids:
        return []
    return list(await db.scalars(select(Alert).where(Alert.id.in_(inserted_ids))))
