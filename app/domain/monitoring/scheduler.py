"""APScheduler wiring for the recurring alert scan (spec §2 Ruling 1).

The FastAPI lifespan (`app.main.create_app`) builds an `AsyncIOScheduler` and registers a wrapper
around `run_scan_once` on a fixed 60-second `IntervalTrigger`. A fixed interval is deliberate for
now: reading `alert_scan_interval_seconds` from `system_settings` at startup would add a query and
a chicken-and-egg with the session factory. A follow-up can make it dynamic.
"""

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.monitoring.scan import run_alert_scan

logger = logging.getLogger(__name__)

ALERT_SCAN_INTERVAL_SECONDS = 60


async def run_scan_once(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Open a dedicated session, run one alert scan, and swallow+log any failure.

    The scheduler has no request scope, so this opens its own `AsyncSession`. A scan that raises
    must never propagate out of the job and stop the scheduler (APScheduler also catches job
    exceptions, but the explicit guard is clearer and keeps the behaviour independent of that).
    """
    try:
        async with session_factory() as session:
            await run_alert_scan(session)
    except Exception:
        logger.exception("alert scan failed", extra={"event": "alert_scan_failed"})


def build_alert_scheduler(scan_job: Callable[[], Awaitable[None]]) -> AsyncIOScheduler:
    """Return a started `AsyncIOScheduler` running `scan_job` on the fixed alert-scan interval."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_job,
        trigger=IntervalTrigger(seconds=ALERT_SCAN_INTERVAL_SECONDS),
        id="alert_scan",
        name="alert_scan",
    )
    scheduler.start()
    return scheduler
