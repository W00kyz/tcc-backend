"""Pure alert rules: turn hand-built route/stop snapshots plus resolved thresholds and a
fixed `now` into `AlertDraft`s (spec §2 Ruling 2, RF52). No DB, no I/O, no wall clock — the
driver (`run_alert_scan`) loads state, calls each enabled rule, and does the batched
`INSERT ... ON CONFLICT (dedup_key) DO NOTHING`.

The schedule-based rules (late start, worker absent, late stop) went with the planned times
(spec 2026-10-20-alocacao-automatica §8.7); the release-driven alerts are raised where the
release happens, not by a rule over a snapshot.

Example:
    drafts = rule_no_evidence(routes, thresholds, now, {AlertKind.NO_EVIDENCE: True})
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
    # None for a day-level alert that belongs to no route (POINTS_UNSERVED).
    route_id: uuid.UUID | None
    route_stop_id: uuid.UUID | None = None
    field_worker_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AlertThresholds:
    no_evidence_min: int


@dataclass(frozen=True)
class StopSnapshot:
    route_stop_id: uuid.UUID
    status: str  # RouteStopStatus value
    checked_out_at: datetime | None
    execution_source: str | None  # ExecutionSource value or None (no execution)
    evidence_count: int


@dataclass(frozen=True)
class RouteSnapshot:
    route_id: uuid.UUID
    stops: list[StopSnapshot]


def _overdue(moment: datetime | None, minutes: int, now: datetime) -> bool:
    return moment is not None and moment + timedelta(minutes=minutes) < now


def rule_no_evidence(
    routes: list[RouteSnapshot],
    thresholds: AlertThresholds,
    now: datetime,
    enabled: dict[AlertKind, bool],
) -> list[AlertDraft]:
    """DONE app stop with no evidence past the grace period."""
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
        and _overdue(stop.checked_out_at, thresholds.no_evidence_min, now)
    ]
