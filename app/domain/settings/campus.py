"""Where and when the field work happens (spec 2026-10-20-alocacao-automatica §5.5): the campus
rectangle, the service weekdays and the end of the workday. Pure reads of the settings row — the
allocation refuses a batch on them, and the release rules (B4) reuse them."""

from datetime import date, datetime

from sqlalchemy import ColumnElement, and_
from sqlalchemy.orm import InstrumentedAttribute

from app.core.campus_clock import CAMPUS_TIMEZONE
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
