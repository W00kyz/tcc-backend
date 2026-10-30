"""Rules of outdoor places (spec 2026-10-30-locais-externos §3): a place sits inside the campus,
and no HTTP is spoken here; the router maps the error."""

from app.domain.settings.campus import describe_campus, inside_campus
from app.domain.settings.models import SystemSettings


class OutsideCampus(Exception):  # noqa: N818 - same name and meaning as the allocation's refusal
    """A place was put outside the campus rectangle (`system_settings.campus_*`)."""


def ensure_inside_campus(latitude: float, longitude: float, *, bounds: SystemSettings) -> None:
    """Raises `OutsideCampus` unless (latitude, longitude) is inside the campus rectangle held
    by `bounds` (edges included) — the same rectangle the route allocation checks workers against.

        ensure_inside_campus(-7.2150, -35.9100, bounds=await get_settings_row(db))
    """
    if not inside_campus(bounds, latitude, longitude):
        raise OutsideCampus(
            f"Position ({latitude}, {longitude}) is outside the campus "
            f"{describe_campus(bounds)}; expected a position inside it."
        )
