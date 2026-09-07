"""Etapa 9 — the monitoring surface for the manager dashboard (RF41). For now just the board
snapshot and the SSE stream; the alert-handling endpoints land on this router later."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.alerts_service import (
    AlertAlreadyResolved,
    acknowledge_alert,
    get_alert_rule,
    list_alert_rules,
    list_alerts,
    resolve_alert,
    update_alert_rule,
)
from app.domain.monitoring.board import BoardSnapshot, build_board
from app.domain.monitoring.models import Alert, AlertKind, AlertRule, AlertStatus

router = APIRouter(tags=["monitoring"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Server-side poll interval for the SSE generator (spec §4 — the stream re-derives the board by
# polling `build_board`, it is not event-driven).
_POLL_SECONDS = 2.0
# Silence budget before the stream emits an SSE keep-alive comment. A module constant so a test
# can monkeypatch it down instead of waiting the full interval.
_KEEPALIVE_SECONDS = 15.0

# Fields left out of the change digest. `generated_at` and each position's `age_seconds` advance
# on every poll from clock drift alone, so hashing them would re-emit a snapshot every 2s. The
# digest covers only the meaningful state — routes, alerts, and each fix's coordinates/reported_at.
_DIGEST_EXCLUDE: dict[str, object] = {
    "generated_at": True,
    "worker_positions": {"__all__": {"age_seconds"}},
}


def _snapshot_digest(snapshot: BoardSnapshot) -> str:
    """A stable hash of a `BoardSnapshot`'s change-relevant subset (see `_DIGEST_EXCLUDE`)."""
    payload = snapshot.model_dump(mode="json", exclude=_DIGEST_EXCLUDE)  # type: ignore[arg-type]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.get("/monitoring/board", response_model=BoardSnapshot)
async def get_board(_actor: _Manager, db: _Db) -> BoardSnapshot:
    """RF41 — the coloured route board: every open route with its derived `is_delayed` flag and
    OPEN alert count, the board's alert list, and the latest GPS fix per worker."""
    return await build_board(db, now=datetime.now(UTC))


async def board_event_stream(
    session_factory: async_sessionmaker[AsyncSession],
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[str]:
    """The SSE body for `GET /monitoring/stream` (spec §2 Ruling 4, §4).

    Polls `build_board` every ~2s on a fresh short-lived session — the DB connection is opened
    and closed inside the loop, never held across the wait. Emits an `event: board` frame the
    first time and whenever the change digest moves, and a `: keep-alive` comment after ~15s of
    silence. Returns as soon as `is_disconnected()` reports the client is gone.

    Taken as a parameter (not read off `Request`) so tests can drive it without a live server —
    httpx's ASGITransport buffers the whole response and cannot exercise an open-ended stream."""
    last_digest: str | None = None
    last_emit = time.monotonic()
    while True:
        if await is_disconnected():
            break
        async with session_factory() as session:
            snapshot = await build_board(session, now=datetime.now(UTC))
        digest = _snapshot_digest(snapshot)
        now_mono = time.monotonic()
        if digest != last_digest:
            yield f"event: board\ndata: {snapshot.model_dump_json()}\n\n"
            last_digest = digest
            last_emit = now_mono
        elif now_mono - last_emit >= _KEEPALIVE_SECONDS:
            yield ": keep-alive\n\n"
            last_emit = now_mono
        await asyncio.sleep(_POLL_SECONDS)


@router.get("/monitoring/stream")
async def stream_board(request: Request, _actor: _Manager, db: _Db) -> StreamingResponse:
    """RF41 real-time — the same `BoardSnapshot` as `GET /monitoring/board`, pushed as
    Server-Sent Events whenever its meaningful content changes (spec §2 Ruling 4, §4)."""
    # Auth is done; release the pooled connection the `_Manager` dependency checked out so it
    # is not held idle-in-transaction for the whole (potentially hours-long) streaming response.
    # The stream itself opens its own short-lived sessions inside `board_event_stream`.
    await db.close()
    return StreamingResponse(
        board_event_stream(request.app.state.session_factory, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --- alert lifecycle (spec §2 Ruling 7) -----------------------------------------------------


class AlertOut(BaseModel):
    id: UUID
    kind: AlertKind
    severity: str
    description: str
    route_id: UUID
    route_stop_id: UUID | None
    field_worker_id: UUID | None
    status: AlertStatus
    acknowledged_by: UUID | None
    acknowledged_at: datetime | None
    resolved_by: UUID | None
    resolved_at: datetime | None
    created_at: datetime


class AlertRuleOut(BaseModel):
    rule_kind: AlertKind
    enabled: bool
    threshold_minutes: int | None
    updated_at: datetime | None


class AlertRulePatch(BaseModel):
    enabled: bool | None = None
    # Same bounds as the semantically-identical `SettingsUpdate` tolerance fields — a threshold
    # in minutes, capped at a day.
    threshold_minutes: int | None = Field(default=None, ge=0, le=1440)


def _to_alert_out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=alert.id,
        kind=alert.kind,
        severity=alert.severity.value,
        description=alert.description,
        route_id=alert.route_id,
        route_stop_id=alert.route_stop_id,
        field_worker_id=alert.field_worker_id,
        status=alert.status,
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
        resolved_by=alert.resolved_by,
        resolved_at=alert.resolved_at,
        created_at=alert.created_at,
    )


def _to_rule_out(rule: AlertRule) -> AlertRuleOut:
    return AlertRuleOut(
        rule_kind=rule.rule_kind,
        enabled=rule.enabled,
        threshold_minutes=rule.threshold_minutes,
        updated_at=rule.updated_at,
    )


async def _load_alert(db: AsyncSession, alert_id: UUID) -> Alert:
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Alert "{alert_id}" not found.')
    return alert


@router.get("/alerts", response_model=list[AlertOut])
async def get_alerts(
    _actor: _Manager,
    db: _Db,
    alert_status: Annotated[AlertStatus | None, Query(alias="status")] = None,
    route_id: UUID | None = None,
) -> list[AlertOut]:
    """RF41 — the dashboard's alert list, optionally filtered by `status` and/or `route_id`."""
    rows = await list_alerts(db, status=alert_status, route_id=route_id)
    return [_to_alert_out(alert) for alert in rows]


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge(alert_id: UUID, actor: _Manager, db: _Db) -> AlertOut:
    """Cenário 5 step 7 — a manager takes an OPEN alert. Idempotent on an already
    ACKNOWLEDGED alert; a RESOLVED alert answers 409."""
    alert = await _load_alert(db, alert_id)
    before = {"status": alert.status.value}
    try:
        alert = await acknowledge_alert(db, alert=alert, actor_id=actor.id)
    except AlertAlreadyResolved as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="alert",
        entity_id=alert.id,
        action="acknowledge",
        before=before,
        after={"status": alert.status.value},
    )
    await db.commit()
    await db.refresh(alert)
    return _to_alert_out(alert)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve(alert_id: UUID, actor: _Manager, db: _Db) -> AlertOut:
    """Cenário 5 step 8 — a manager closes an alert from OPEN or ACKNOWLEDGED (idempotent
    on an already RESOLVED alert)."""
    alert = await _load_alert(db, alert_id)
    before = {"status": alert.status.value}
    alert = await resolve_alert(db, alert=alert, actor_id=actor.id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="alert",
        entity_id=alert.id,
        action="resolve",
        before=before,
        after={"status": alert.status.value},
    )
    await db.commit()
    await db.refresh(alert)
    return _to_alert_out(alert)


# --- configurable alert rules (spec §2 Ruling 8, RF50) ------------------------------------


@router.get("/alert-rules", response_model=list[AlertRuleOut])
async def get_alert_rules(_actor: _Manager, db: _Db) -> list[AlertRuleOut]:
    return [_to_rule_out(rule) for rule in await list_alert_rules(db)]


@router.patch("/alert-rules/{rule_kind}", response_model=AlertRuleOut)
async def patch_alert_rule(
    rule_kind: str, body: AlertRulePatch, actor: _Manager, db: _Db
) -> AlertRuleOut:
    """Toggle a rule on/off or override its threshold (RF50). An unknown `rule_kind` answers
    404 (the path is a free string, not the path enum, so the check is ours to make)."""
    try:
        kind = AlertKind(rule_kind)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Alert rule "{rule_kind}" not found.'
        ) from exc
    rule = await get_alert_rule(db, kind)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Alert rule "{rule_kind}" not found.')
    before = {"enabled": rule.enabled, "threshold_minutes": rule.threshold_minutes}
    rule = await update_alert_rule(
        db,
        rule=rule,
        enabled=body.enabled,
        threshold_minutes=body.threshold_minutes,
        actor_id=actor.id,
    )
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="alert_rule",
        entity_id=_RULE_AUDIT_IDS[rule.rule_kind],
        action="update_alert_rule",
        before=before,
        after={"enabled": rule.enabled, "threshold_minutes": rule.threshold_minutes},
    )
    await db.commit()
    await db.refresh(rule)
    return _to_rule_out(rule)


# `alert_rules` is keyed by the enum, not a UUID; the audit trail's `entity_id` is a non-null
# UUID column, so map each rule kind to a fixed sentinel (same trick as the settings row).
_RULE_AUDIT_IDS: dict[AlertKind, UUID] = {
    kind: UUID(int=index + 1) for index, kind in enumerate(AlertKind)
}
