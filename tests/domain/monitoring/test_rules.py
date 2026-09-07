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
    rule_late_start,
    rule_late_stop,
    rule_no_evidence,
    rule_worker_absent,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
THRESHOLDS = AlertThresholds(
    late_start_min=15, absence_min=45, late_stop_min=20, no_evidence_min=30
)
ALL_ON: dict[AlertKind, bool] = {}


def _route(**overrides: object) -> RouteSnapshot:
    base: dict[str, object] = {
        "route_id": uuid.uuid4(),
        "status": "PLANNED",
        "scheduled_start_at": NOW - timedelta(minutes=20),
        "field_worker_id": uuid.uuid4(),
        "stops": [],
    }
    base.update(overrides)
    return RouteSnapshot(**base)  # type: ignore[arg-type]


def _stop(**overrides: object) -> StopSnapshot:
    base: dict[str, object] = {
        "route_stop_id": uuid.uuid4(),
        "status": "PENDING",
        "expected_arrival_to": NOW - timedelta(minutes=25),
        "checked_out_at": NOW - timedelta(minutes=40),
        "execution_source": "APP",
        "evidence_count": 0,
        "has_manual_completion": False,
    }
    base.update(overrides)
    return StopSnapshot(**base)  # type: ignore[arg-type]


# --- rule_late_start ---------------------------------------------------------


def test_late_start_fires_inside_threshold() -> None:
    route = _route(scheduled_start_at=NOW - timedelta(minutes=16))
    drafts = rule_late_start([route], THRESHOLDS, NOW, ALL_ON)
    assert len(drafts) == 1
    assert drafts[0].dedup_key == f"LATE_START:{route.route_id}"
    assert drafts[0].severity == AlertSeverity.WARNING
    assert drafts[0].field_worker_id == route.field_worker_id
    assert drafts[0].route_stop_id is None


@pytest.mark.parametrize(
    "route",
    [
        _route(scheduled_start_at=NOW - timedelta(minutes=10)),  # outside threshold
        _route(status="IN_PROGRESS"),  # wrong status
        _route(scheduled_start_at=None),  # no schedule
    ],
)
def test_late_start_silent(route: RouteSnapshot) -> None:
    assert rule_late_start([route], THRESHOLDS, NOW, ALL_ON) == []


def test_late_start_disabled() -> None:
    route = _route(scheduled_start_at=NOW - timedelta(minutes=60))
    assert rule_late_start([route], THRESHOLDS, NOW, {AlertKind.LATE_START: False}) == []


# --- rule_worker_absent -----------------------------------------------------


def test_worker_absent_fires_inside_threshold() -> None:
    route = _route(scheduled_start_at=NOW - timedelta(minutes=46))
    drafts = rule_worker_absent([route], THRESHOLDS, NOW, ALL_ON)
    assert len(drafts) == 1
    assert drafts[0].dedup_key == f"WORKER_ABSENT:{route.route_id}"
    assert drafts[0].severity == AlertSeverity.CRITICAL


@pytest.mark.parametrize(
    "route",
    [
        _route(scheduled_start_at=NOW - timedelta(minutes=30)),  # past late_start, not absence
        _route(status="DONE"),
        _route(scheduled_start_at=None),
    ],
)
def test_worker_absent_silent(route: RouteSnapshot) -> None:
    assert rule_worker_absent([route], THRESHOLDS, NOW, ALL_ON) == []


def test_worker_absent_disabled() -> None:
    route = _route(scheduled_start_at=NOW - timedelta(minutes=90))
    got = rule_worker_absent([route], THRESHOLDS, NOW, {AlertKind.WORKER_ABSENT: False})
    assert got == []


# --- rule_late_stop -------------------------------------------------------


def test_late_stop_fires_inside_threshold() -> None:
    stop = _stop(expected_arrival_to=NOW - timedelta(minutes=21))
    route = _route(status="IN_PROGRESS", stops=[stop])
    drafts = rule_late_stop([route], THRESHOLDS, NOW, ALL_ON)
    assert len(drafts) == 1
    assert drafts[0].dedup_key == f"LATE_STOP:{stop.route_stop_id}"
    assert drafts[0].severity == AlertSeverity.WARNING
    assert drafts[0].route_stop_id == stop.route_stop_id
    assert drafts[0].field_worker_id == route.field_worker_id


@pytest.mark.parametrize(
    ("route_status", "stop_kwargs"),
    [
        ("IN_PROGRESS", {"expected_arrival_to": NOW - timedelta(minutes=10)}),  # outside
        ("PLANNED", {}),  # wrong route status
        ("IN_PROGRESS", {"status": "DONE"}),  # wrong stop status
        ("IN_PROGRESS", {"expected_arrival_to": None}),  # no window
    ],
)
def test_late_stop_silent(route_status: str, stop_kwargs: dict[str, object]) -> None:
    route = _route(status=route_status, stops=[_stop(**stop_kwargs)])
    assert rule_late_stop([route], THRESHOLDS, NOW, ALL_ON) == []


def test_late_stop_disabled() -> None:
    route = _route(status="IN_PROGRESS", stops=[_stop()])
    assert rule_late_stop([route], THRESHOLDS, NOW, {AlertKind.LATE_STOP: False}) == []


# --- rule_no_evidence ----------------------------------------------------


def test_no_evidence_fires_inside_threshold() -> None:
    stop = _stop(status="DONE", checked_out_at=NOW - timedelta(minutes=31))
    route = _route(status="IN_PROGRESS", stops=[stop])
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
        {"status": "DONE", "execution_source": "MANAGER_MANUAL"},  # not an app stop
        {"status": "DONE", "execution_source": None},  # no execution
        {"status": "DONE", "evidence_count": 2},  # has evidence
        {"status": "DONE", "has_manual_completion": True},  # manual completion
        {"status": "DONE", "checked_out_at": None},  # never checked out
    ],
)
def test_no_evidence_silent(stop_kwargs: dict[str, object]) -> None:
    route = _route(stops=[_stop(**stop_kwargs)])
    assert rule_no_evidence([route], THRESHOLDS, NOW, ALL_ON) == []


def test_no_evidence_disabled() -> None:
    route = _route(stops=[_stop(status="DONE")])
    assert rule_no_evidence([route], THRESHOLDS, NOW, {AlertKind.NO_EVIDENCE: False}) == []
