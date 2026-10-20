"""The campus wall clock (UFCG, Campina Grande).

Campus "today" and "now" always come from here, never from `date.today()` or a bare UTC date:
the server runs in UTC, three hours ahead of the campus.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Campina Grande observes America/Fortaleza (UTC-3, no daylight saving since 2019).
CAMPUS_TIMEZONE = ZoneInfo("America/Fortaleza")


def campus_wall_clock(now: datetime) -> datetime:
    """`now` (aware) as campus wall-clock time tagged UTC — the campus hour, not a real instant.

    >>> campus_wall_clock(datetime(2026, 9, 6, 12, 10, tzinfo=UTC))
    datetime.datetime(2026, 9, 6, 9, 10, tzinfo=datetime.timezone.utc)
    """
    return now.astimezone(CAMPUS_TIMEZONE).replace(tzinfo=UTC)


def campus_today(now: datetime) -> date:
    """The calendar day on campus. `now.date()` on a UTC instant is already tomorrow from
    21:00 local, which put the next day's routes on the board and in the alert scan early."""
    return now.astimezone(CAMPUS_TIMEZONE).date()
