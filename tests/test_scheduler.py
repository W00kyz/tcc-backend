"""The alert-scan scheduler is wired into the app lifespan (spec §2 Ruling 1): off when
`alerts_scheduler_enabled` is False, and on (one registered job) when it is True."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.domain.monitoring import scheduler as scheduler_module
from app.domain.monitoring.scheduler import build_scheduler, run_scan_once
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.object_store import FakeObjectStore


def _make_app(settings: Settings) -> FastAPI:
    return create_app(settings=settings, mailer=RecordingMailer(), object_store=FakeObjectStore())


def test_scheduler_not_started_when_disabled(test_settings: Settings) -> None:
    app = _make_app(test_settings)  # fixture already has alerts_scheduler_enabled=False
    with TestClient(app, client=("127.0.0.1", 50000)):
        assert getattr(app.state, "alert_scheduler", None) is None


def test_scheduler_starts_with_one_job_when_enabled(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"alerts_scheduler_enabled": True})
    app = _make_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000)):
        scheduler = app.state.alert_scheduler
        assert scheduler.running is True
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "alert_scan"
    # Lifespan exit shuts it down.
    assert app.state.alert_scheduler.running is False


@pytest.mark.parametrize(
    ("alerts", "retention", "expected"),
    [
        (False, False, set()),
        (True, False, {"alert_scan"}),
        (False, True, {"retention_purge"}),
        (True, True, {"alert_scan", "retention_purge"}),
    ],
)
async def test_build_scheduler_registers_jobs_per_flag(
    alerts: bool, retention: bool, expected: set[str]
) -> None:
    async def noop() -> None:
        return None

    scheduler = build_scheduler(
        scan_job=noop if alerts else None,
        purge_job=noop if retention else None,
    )
    try:
        assert {job.id for job in scheduler.get_jobs()} == expected
    finally:
        scheduler.shutdown(wait=False)


async def test_run_scan_once_swallows_and_logs_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[bool] = []

    @asynccontextmanager
    async def fake_session_ctx() -> AsyncIterator[object]:
        opened.append(True)
        yield object()

    async def boom(session: object) -> None:
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scheduler_module, "run_alert_scan", boom)

    logged: list[str] = []
    monkeypatch.setattr(
        scheduler_module.logger, "exception", lambda msg, *a, **kw: logged.append(msg)
    )

    # A scan that raises must not propagate out of the job wrapper.
    await run_scan_once(fake_session_ctx)  # type: ignore[arg-type]

    assert opened == [True]
    assert logged == ["alert scan failed"]
