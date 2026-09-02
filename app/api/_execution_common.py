"""Shared route/worker resolution for the check-in (RF29) and check-out (RF31) endpoints.

Both endpoints look up the field worker for the caller and the route to act against — the
one owning a chosen stop on a re-submit, else the worker's started route for today. That
logic is identical; only the HTTP status/message differ, so this module raises plain
exceptions and each endpoint maps them to its own `HTTPException`."""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker
from app.domain.identity.models import User
from app.domain.routing.models import Route, RouteStatus, RouteStop


class NoActiveRoute(Exception):
    """The worker has no IN_PROGRESS route for today and passed no explicit `route_stop_id`."""


class RouteStopMissing(Exception):
    """A re-submit named a `route_stop_id` that does not exist."""


async def current_worker(db: AsyncSession, user: User) -> FieldWorker | None:
    """The field worker profile for the caller, or `None` (the endpoint answers 403)."""
    worker: FieldWorker | None = await db.scalar(
        select(FieldWorker).where(FieldWorker.user_id == user.id)
    )
    return worker


async def resolve_route(db: AsyncSession, *, worker_id: UUID, route_stop_id: UUID | None) -> Route:
    """The route to act against: the one owning the chosen stop on a re-submit, else the
    worker's started route for today."""
    if route_stop_id is not None:
        stop = await db.get(RouteStop, route_stop_id)
        if stop is None:
            raise RouteStopMissing(str(route_stop_id))
        route = await db.get(Route, stop.route_id)
        assert route is not None
        return route

    started_route: Route | None = await db.scalar(
        select(Route).where(
            Route.field_worker_id == worker_id,
            Route.route_date == date.today(),
            Route.status == RouteStatus.IN_PROGRESS,
        )
    )
    if started_route is None:
        raise NoActiveRoute
    return started_route
