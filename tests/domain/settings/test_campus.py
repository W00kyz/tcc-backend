"""The campus rules the allocation guards on: the UFCG rectangle, the service weekdays and the end
of the workday in campus local time (spec 2026-10-20-alocacao-automatica §5.5)."""

from datetime import UTC, date, datetime, time

import pytest
from app.domain.settings.campus import inside_campus, is_service_day, workday_over
from app.domain.settings.models import SystemSettings


def _settings() -> SystemSettings:
    # Not flushed, so the column defaults are not applied: spell out the ones under test.
    return SystemSettings(
        campus_min_lat=-7.2250,
        campus_max_lat=-7.2050,
        campus_min_lng=-35.9200,
        campus_max_lng=-35.8980,
        service_weekdays=[0, 1, 2, 3, 4],
        workday_end_time=time(17, 0),
    )


@pytest.mark.parametrize(
    ("lat", "lng", "inside"),
    [
        (-7.2150, -35.9100, True),
        (-7.2250, -35.9200, True),  # the corners belong to the campus
        (-7.2050, -35.8980, True),
        (-7.2300, -35.9100, False),  # south of the campus
        (-7.2150, -35.8800, False),  # east of the campus
        (-7.2000, -35.9300, False),
    ],
)
def test_inside_campus_is_the_rectangle(lat: float, lng: float, inside: bool) -> None:
    assert inside_campus(_settings(), lat, lng) is inside


def test_weekdays_are_service_days_and_the_weekend_is_not() -> None:
    settings = _settings()

    assert is_service_day(settings, date(2026, 10, 19)) is True  # Monday
    assert is_service_day(settings, date(2026, 10, 23)) is True  # Friday
    assert is_service_day(settings, date(2026, 10, 24)) is False  # Saturday
    assert is_service_day(settings, date(2026, 10, 25)) is False  # Sunday


def test_service_days_follow_the_settings() -> None:
    settings = _settings()
    settings.service_weekdays = [5]

    assert is_service_day(settings, date(2026, 10, 24)) is True  # Saturday
    assert is_service_day(settings, date(2026, 10, 19)) is False  # Monday


def test_workday_ends_at_the_campus_local_end_time() -> None:
    settings = _settings()

    # Campina Grande is UTC-3: 19:59 UTC is 16:59 on campus, 20:00 UTC is 17:00.
    assert workday_over(settings, datetime(2026, 10, 20, 19, 59, tzinfo=UTC)) is False
    assert workday_over(settings, datetime(2026, 10, 20, 20, 0, tzinfo=UTC)) is True
    # 17:30 UTC is 14:30 on campus — a UTC comparison would already call the day over.
    assert workday_over(settings, datetime(2026, 10, 20, 17, 30, tzinfo=UTC)) is False
