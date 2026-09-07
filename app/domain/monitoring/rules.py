"""Pure alert rules: turn hand-built route/stop snapshots plus resolved thresholds and a
fixed `now` into `AlertDraft`s (spec §2 Ruling 2, RF52). No DB, no I/O, no wall clock — the
driver (`run_alert_scan`) loads state, calls each enabled rule, and does the batched
`INSERT ... ON CONFLICT (dedup_key) DO NOTHING`.

Example:
    drafts = rule_late_start(routes, thresholds, now, {AlertKind.LATE_START: True})
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.monitoring.models import AlertKind, AlertSeverity


@dataclass(frozen=True)
class AlertDraft:
    kind: AlertKind
    severity: AlertSeverity
    dedup_key: str
    description: str
    route_id: uuid.UUID
    route_stop_id: uuid.UUID | None = None
    field_worker_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AlertThresholds:
    late_start_min: int
    absence_min: int
    late_stop_min: int
    no_evidence_min: int


@dataclass(frozen=True)
class StopSnapshot:
    route_stop_id: uuid.UUID
    status: str  # RouteStopStatus value
    expected_arrival_to: datetime | None
    checked_out_at: datetime | None
    execution_source: str | None  # ExecutionSource value or None (no execution)
    evidence_count: int
    has_manual_completion: bool


@dataclass(frozen=True)
class RouteSnapshot:
    route_id: uuid.UUID
    status: str  # RouteStatus value
    scheduled_start_at: datetime | None
    field_worker_id: uuid.UUID
    stops: list[StopSnapshot]


def _overdue(moment: datetime | None, minutes: int, now: datetime) -> bool:
    return moment is not None and moment + timedelta(minutes=minutes) < now


def rule_late_start(
    routes: list[RouteSnapshot],
    thresholds: AlertThresholds,
    now: datetime,
    enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """Route still PLANNED past its scheduled start + `late_start_min` tolerance."""
    if not enabled.get(AlertKind.LATE_START, True):
        return []
    return [
        AlertDraft(
            kind=AlertKind.LATE_START,
            severity=AlertSeverity.WARNING,
            dedup_key=f"LATE_START:{route.route_id}",
            description=(
                f"Rota não iniciada há mais de {thresholds.late_start_min} min do horário previsto."
            ),
            route_id=route.route_id,
            field_worker_id=route.field_worker_id,
        )
        for route in routes
        if route.status == "PLANNED"
        and _overdue(route.scheduled_start_at, thresholds.late_start_min, now)
    ]


def rule_worker_absent(
    routes: list[RouteSnapshot],
    thresholds: AlertThresholds,
    now: datetime,
    enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """Route still PLANNED past its scheduled start + the larger `absence_min` tolerance
    (RF52). Only records the CRITICAL alert — no auto reassign/cancel."""
    if not enabled.get(AlertKind.WORKER_ABSENT, True):
        return []
    return [
        AlertDraft(
            kind=AlertKind.WORKER_ABSENT,
            severity=AlertSeverity.CRITICAL,
            dedup_key=f"WORKER_ABSENT:{route.route_id}",
            description=(
                "Profissional possivelmente ausente: rota não iniciada há mais de "
                f"{thresholds.absence_min} min do horário previsto."
            ),
            route_id=route.route_id,
            field_worker_id=route.field_worker_id,
        )
        for route in routes
        if route.status == "PLANNED"
        and _overdue(route.scheduled_start_at, thresholds.absence_min, now)
    ]


def rule_late_stop(
    routes: list[RouteSnapshot],
    thresholds: AlertThresholds,
    now: datetime,
    enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """PENDING stop on an IN_PROGRESS route past its arrival window + `late_stop_min`."""
    if not enabled.get(AlertKind.LATE_STOP, True):
        return []
    return [
        AlertDraft(
            kind=AlertKind.LATE_STOP,
            severity=AlertSeverity.WARNING,
            dedup_key=f"LATE_STOP:{stop.route_stop_id}",
            description=(
                f"Parada pendente há mais de {thresholds.late_stop_min} min além "
                "da janela de chegada prevista."
            ),
            route_id=route.route_id,
            route_stop_id=stop.route_stop_id,
            field_worker_id=route.field_worker_id,
        )
        for route in routes
        if route.status == "IN_PROGRESS"
        for stop in route.stops
        if stop.status == "PENDING"
        and _overdue(stop.expected_arrival_to, thresholds.late_stop_min, now)
    ]


def rule_no_evidence(
    routes: list[RouteSnapshot],
    thresholds: AlertThresholds,
    now: datetime,
    enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """DONE app stop with no evidence and no manual completion past the grace period."""
    if not enabled.get(AlertKind.NO_EVIDENCE, True):
        return []
    return [
        AlertDraft(
            kind=AlertKind.NO_EVIDENCE,
            severity=AlertSeverity.INFO,
            dedup_key=f"NO_EVIDENCE:{stop.route_stop_id}",
            description=(
                f"Parada concluída pelo app há mais de {thresholds.no_evidence_min} min "
                "sem evidência anexada."
            ),
            route_id=route.route_id,
            route_stop_id=stop.route_stop_id,
        )
        for route in routes
        for stop in route.stops
        if stop.status == "DONE"
        and stop.execution_source == "APP"
        and stop.evidence_count == 0
        and not stop.has_manual_completion
        and _overdue(stop.checked_out_at, thresholds.no_evidence_min, now)
    ]
