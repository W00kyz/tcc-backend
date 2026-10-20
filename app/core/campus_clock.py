"""The campus wall clock (UFCG, Campina Grande).

IMPORTANT: arrival windows (`route_stops.expected_arrival_*`, and template times materialised by
`routing.service._combine_date_time`) are stored as campus wall-clock times tagged UTC — the
dashboard sends "08:00" as 08:00Z and the mobile app reads `.hour` straight off the timestamp
(mobile `route_screen.dart`, INTENTIONAL). The stored value is NOT a real UTC instant.

So anything compared with a window must be expressed the same way: `campus_wall_clock(now)`,
not `now`. Comparing with the real UTC instant made late-stop alerts fire three hours early.
Real instants (check-ins, `scheduled_start_at`, `created_at`) keep using plain UTC.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Campina Grande observes America/Fortaleza (UTC-3, no daylight saving since 2019).
CAMPUS_TIMEZONE = ZoneInfo("America/Fortaleza")


def campus_wall_clock(now: datetime) -> datetime:
    """`now` (aware) as campus wall-clock time tagged UTC, comparable with arrival windows.

    >>> campus_wall_clock(datetime(2026, 9, 6, 12, 10, tzinfo=UTC))
    datetime.datetime(2026, 9, 6, 9, 10, tzinfo=datetime.timezone.utc)
    """
    return now.astimezone(CAMPUS_TIMEZONE).replace(tzinfo=UTC)


def campus_today(now: datetime) -> date:
    """The calendar day on campus. `now.date()` on a UTC instant is already tomorrow from
    21:00 local, which put the next day's routes on the board and in the alert scan early."""
    return now.astimezone(CAMPUS_TIMEZONE).date()
