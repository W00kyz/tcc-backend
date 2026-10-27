"""Runs the release scan once, on demand, instead of waiting for the scheduler's next minute.

Usage: uv run python -m app.scripts.run_release_scan [--now 2026-10-20T15:00:00-03:00]

The same pass the scheduler runs every 60 s (spec 2026-10-20-alocacao-automatica §7.2-§7.4):
batches of workers outside the campus or silent for too long go back to the pool, and past the
end of the workday every open route closes. `--now` is the instant the scan judges against
(default: the real clock); a value without an offset is campus wall-clock time. The e2e suite
uses it, with the stack's container, to release a silent worker's batch without waiting:

    docker compose exec backend uv run python -m app.scripts.run_release_scan
"""

import argparse
import asyncio
from datetime import UTC, datetime

from app.core.campus_clock import CAMPUS_TIMEZONE
from app.core.config import get_settings
from app.db import models_registry  # noqa: F401 — every model, so all FKs resolve on flush
from app.db.session import build_engine, build_session_factory
from app.domain.monitoring.release_scan import run_release_scan


def parse_now(value: str | None) -> datetime:
    """The scan's `now` from the `--now` argument: the real clock when absent; an ISO datetime
    without an offset is read as campus local time.

    >>> parse_now("2026-10-20T10:00")
    datetime.datetime(2026, 10, 20, 10, 0, tzinfo=zoneinfo.ZoneInfo(key='America/Fortaleza'))
    """
    if value is None:
        return datetime.now(UTC)
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=CAMPUS_TIMEZONE)


async def release_once(database_url: str, now: datetime) -> int:
    """One release scan on its own engine and session (the scan commits); returns how many new
    alerts it raised.

        raised = await release_once(get_settings().database_url, datetime.now(UTC))
    """
    engine = build_engine(database_url)
    try:
        async with build_session_factory(engine)() as db:
            return len(await run_release_scan(db, now=now))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the release scan once.")
    parser.add_argument(
        "--now", help="ISO datetime the scan judges against (default: now; no offset = campus)"
    )
    now = parse_now(parser.parse_args(argv).now)
    raised = asyncio.run(release_once(get_settings().database_url, now))
    print(f"Release scan at {now.isoformat()}: {raised} new alert(s).")


if __name__ == "__main__":
    main()
