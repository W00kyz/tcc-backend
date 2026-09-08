"""APScheduler wiring for the recurring background jobs (spec §2 Ruling 1, spec §D / RNF10).

The FastAPI lifespan (`app.main.create_app`) builds a single `AsyncIOScheduler` when either
background job is enabled and registers whichever are:

- the alert scan, on a fixed 60-second `IntervalTrigger` (`alerts_scheduler_enabled`). A fixed
  interval is deliberate: reading `alert_scan_interval_seconds` from `system_settings` at startup
  would add a query and a chicken-and-egg with the session factory. A follow-up can make it
  dynamic.
- the retention purge, daily at 03:00 on a `CronTrigger` (`retention_purge_enabled`).

Each job opens its own `AsyncSession` — the scheduler has no request scope — and swallows+logs
any failure so one bad run never stops the scheduler.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.object_store import MinioObjectStore
from app.domain.monitoring.scan import run_alert_scan
from app.domain.retention.purge import run_retention_purge

logger = logging.getLogger(__name__)

ALERT_SCAN_INTERVAL_SECONDS = 60
RETENTION_PURGE_HOUR = 3


async def run_scan_once(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Open a dedicated session, run one alert scan, and swallow+log any failure.

    The scheduler has no request scope, so this opens its own `AsyncSession`. A scan that raises
    must never propagate out of the job and stop the scheduler (APScheduler also catches job
    exceptions, but the explicit guard is clearer and keeps the behaviour independent of that).
    """
    try:
        async with session_factory() as session:
            # No `push` argument: the scheduler has no app state, so `run_alert_scan` falls back
            # to its own `LoggingPushSender`. Real FCM/APNs wiring is documented future work.
            await run_alert_scan(session)
    except Exception:
        logger.exception("alert scan failed", extra={"event": "alert_scan_failed"})


async def run_retention_purge_once(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Open a dedicated session + object store, run one retention purge, swallow+log failures."""
    try:
        object_store = MinioObjectStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket_evidence,
            secure=settings.minio_secure,
        )
        async with session_factory() as session:
            await run_retention_purge(session, object_store=object_store, now=datetime.now(UTC))
    except Exception:
        logger.exception("retention purge failed", extra={"event": "retention_purge_failed"})


def build_scheduler(
    *,
    scan_job: Callable[[], Awaitable[None]] | None = None,
    purge_job: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIOScheduler:
    """Return a started `AsyncIOScheduler` with whichever of the two background jobs was given."""
    scheduler = AsyncIOScheduler()
    if scan_job is not None:
        scheduler.add_job(
            scan_job,
            trigger=IntervalTrigger(seconds=ALERT_SCAN_INTERVAL_SECONDS),
            id="alert_scan",
            name="alert_scan",
        )
    if purge_job is not None:
        scheduler.add_job(
            purge_job,
            trigger=CronTrigger(hour=RETENTION_PURGE_HOUR, minute=0),
            id="retention_purge",
            name="retention_purge",
        )
    scheduler.start()
    return scheduler


def build_alert_scheduler(scan_job: Callable[[], Awaitable[None]]) -> AsyncIOScheduler:
    """Backwards-compatible shim: a started scheduler running only the alert-scan job."""
    return build_scheduler(scan_job=scan_job)
