"""The campus wall-clock convention: arrival windows are stored as Campina Grande wall-clock
times tagged UTC (the dashboard's RouteBuilderPage sends "08:00" as 08:00Z, the mobile app reads
.hour straight off the timestamp). Anything compared with them has to be expressed the same way."""

from datetime import UTC, date, datetime

from app.core.campus_clock import campus_today, campus_wall_clock


def test_wall_clock_is_campus_time_tagged_utc() -> None:
    # 12:10 UTC is 09:10 in Campina Grande (UTC-3, no daylight saving since 2019).
    real_now = datetime(2026, 9, 6, 12, 10, tzinfo=UTC)

    assert campus_wall_clock(real_now) == datetime(2026, 9, 6, 9, 10, tzinfo=UTC)


def test_campus_today_does_not_roll_over_at_utc_midnight() -> None:
    # 22:30 on the 6th in Campina Grande is already the 7th in UTC.
    real_now = datetime(2026, 9, 7, 1, 30, tzinfo=UTC)

    assert campus_today(real_now) == date(2026, 9, 6)
