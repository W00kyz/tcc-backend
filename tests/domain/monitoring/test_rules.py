"""Unit tests for the pure alert rules (spec §2 Ruling 2). No DB, no fixtures — every
snapshot is built inline and `now` is passed explicitly."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.monitoring.models import AlertKind, AlertSeverity
from app.domain.monitoring.rules import (
    AlertThresholds,
    RouteSnapshot,
    StopSnapshot,
    rule_no_evidence,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
THRESHOLDS = AlertThresholds(no_evidence_min=30)
ALL_ON: dict[AlertKind, bool] = {}


def _route(stops: list[StopSnapshot]) -> RouteSnapshot:
    return RouteSnapshot(route_id=uuid.uuid4(), stops=stops)


def _stop(**overrides: object) -> StopSnapshot:
    base: dict[str, object] = {
        "route_stop_id": uuid.uuid4(),
        "status": "PENDING",
        "checked_out_at": NOW - timedelta(minutes=40),
        "execution_source": "APP",
        "evidence_count": 0,
    }
    base.update(overrides)
    return StopSnapshot(**base)  # type: ignore[arg-type]


# --- rule_no_evidence ----------------------------------------------------


def test_no_evidence_fires_inside_threshold() -> None:
    stop = _stop(status="DONE", checked_out_at=NOW - timedelta(minutes=31))
    route = _route([stop])
    drafts = rule_no_evidence([route], THRESHOLDS, NOW, ALL_ON)
    assert len(drafts) == 1
    assert drafts[0].dedup_key == f"NO_EVIDENCE:{stop.route_stop_id}"
    assert drafts[0].severity == AlertSeverity.INFO
    assert drafts[0].field_worker_id is None


@pytest.mark.parametrize(
    "stop_kwargs",
    [
        {"status": "DONE", "checked_out_at": NOW - timedelta(minutes=10)},  # outside
        {"status": "PENDING"},  # wrong status
        {"status": "DONE", "execution_source": "APPEAL"},  # accepted appeal, not an app stop
        {"status": "DONE", "execution_source": None},  # no execution
        {"status": "DONE", "evidence_count": 2},  # has evidence
        {"status": "DONE", "checked_out_at": None},  # never checked out
    ],
)
def test_no_evidence_silent(stop_kwargs: dict[str, object]) -> None:
    route = _route([_stop(**stop_kwargs)])
    assert rule_no_evidence([route], THRESHOLDS, NOW, ALL_ON) == []


def test_no_evidence_disabled() -> None:
    route = _route([_stop(status="DONE")])
    assert rule_no_evidence([route], THRESHOLDS, NOW, {AlertKind.NO_EVIDENCE: False}) == []
