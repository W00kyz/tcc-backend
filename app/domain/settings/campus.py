"""Where and when the field work happens (spec 2026-10-20-alocacao-automatica §5.5): the campus
rectangle, the service weekdays and the start and end of the workday. Pure reads of the settings
row — the allocation refuses a batch on them, and the release rules (B4) reuse them."""

from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, and_
from sqlalchemy.orm import InstrumentedAttribute

from app.core.campus_clock import CAMPUS_TIMEZONE, campus_today
from app.domain.settings.models import SystemSettings


def inside_campus(settings: SystemSettings, lat: float, lng: float) -> bool:
    """Whether (lat, lng) is inside the campus rectangle, edges included.

    >>> inside_campus(settings, -7.2150, -35.9100)
    True
    """
    return (
        settings.campus_min_lat <= lat <= settings.campus_max_lat
        and settings.campus_min_lng <= lng <= settings.campus_max_lng
    )


def describe_campus(settings: SystemSettings) -> str:
    """The campus rectangle as text, for the message of a position that falls outside it.

    >>> describe_campus(settings)
    '[-7.225, -7.205] x [-35.92, -35.898]'
    """
    return (
        f"[{settings.campus_min_lat}, {settings.campus_max_lat}] x "
        f"[{settings.campus_min_lng}, {settings.campus_max_lng}]"
    )


def inside_campus_clause(
    settings: SystemSettings, lat: InstrumentedAttribute[float], lng: InstrumentedAttribute[float]
) -> ColumnElement[bool]:
    """`inside_campus` as a SQL predicate over two mapped columns, to filter positions in SQL.

    >>> select(WorkerPosition).where(inside_campus_clause(settings, WorkerPosition.latitude,
    ...                                                   WorkerPosition.longitude))
    """
    return and_(
        lat.between(settings.campus_min_lat, settings.campus_max_lat),
        lng.between(settings.campus_min_lng, settings.campus_max_lng),
    )


def is_service_day(settings: SystemSettings, day: date) -> bool:
    """Whether `day` (a campus day) is one of the service weekdays — weekdays only by default,
    no holiday calendar.

    >>> is_service_day(settings, date(2026, 10, 24))  # a Saturday
    False
    """
    return day.weekday() in settings.service_weekdays


def workday_over(settings: SystemSettings, now: datetime) -> bool:
    """Whether the campus wall clock at `now` (aware) has reached `workday_end_time`. The end
    time is campus local; comparing it with the UTC hour would close the day at 14:00.

    >>> workday_over(settings, datetime(2026, 10, 20, 20, 0, tzinfo=UTC))  # 17:00 on campus
    True
    """
    return now.astimezone(CAMPUS_TIMEZONE).time() >= settings.workday_end_time


def workday_not_started(settings: SystemSettings, now: datetime) -> bool:
    """Whether the campus wall clock at `now` (aware) is still before `workday_start_time`.
    The start time is campus local, like the end time.

    >>> workday_not_started(settings, datetime(2026, 10, 20, 9, 59, tzinfo=UTC))  # 06:59 on campus
    True
    """
    return now.astimezone(CAMPUS_TIMEZONE).time() < settings.workday_start_time


def start_opens_at(settings: SystemSettings, now: datetime) -> datetime | None:
    """The UTC instant at which starting the route becomes allowed today, for the app to show a
    countdown. `None` when it is already open, or when today is not a service day (there is no
    opening to wait for).

    >>> start_opens_at(settings, datetime(2026, 10, 20, 9, 55, tzinfo=UTC))  # 06:55 on campus
    datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.timezone.utc)
    """
    today = campus_today(now)
    if not is_service_day(settings, today) or not workday_not_started(settings, now):
        return None
    opening = datetime.combine(today, settings.workday_start_time, tzinfo=CAMPUS_TIMEZONE)
    return opening.astimezone(UTC)
