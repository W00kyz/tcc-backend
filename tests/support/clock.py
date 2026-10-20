"""The campus calendar day for tests that arrange "today's" route.

The API files a route under the campus day (`app.api.deps.get_campus_today`); `date.today()` on a
UTC machine (CI, the API container) is already tomorrow from 21:00 in Campina Grande, so a route
dated that way is not "today" for the code under test and the test fails every night.
"""

from datetime import UTC, date, datetime

from app.core.campus_clock import campus_today


def today_on_campus() -> date:
    """Example: ``Route(field_worker_id=worker.id, route_date=today_on_campus())``"""
    return campus_today(datetime.now(UTC))
