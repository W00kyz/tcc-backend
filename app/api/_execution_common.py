"""Shared route/worker resolution for the check-in (RF29) and check-out (RF31) endpoints.

Both endpoints look up the field worker for the caller and the route to act against — the
one owning a chosen stop on a re-submit, else the worker's started route for today. That
logic is identical; only the HTTP status/message differ, so this module raises plain
exceptions and each endpoint maps them to its own `HTTPException`. The 409 a late scan of a
released stop gets is shared too, with the evidence uploads."""

from datetime import date
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker
from app.domain.execution.service import StopReleased
from app.domain.identity.models import User
from app.domain.routing.models import Route, RouteStatus, RouteStop


class NoActiveRoute(Exception):
    """The worker has no started (IN_PROGRESS, PAUSED or DONE) route for today and passed no
    explicit `route_stop_id`."""


class RouteStopMissing(Exception):
    """A re-submit named a `route_stop_id` that does not exist."""


async def current_worker(db: AsyncSession, user: User) -> FieldWorker | None:
    """The field worker profile for the caller, or `None` (the endpoint answers 403)."""
    worker: FieldWorker | None = await db.scalar(
        select(FieldWorker).where(FieldWorker.user_id == user.id)
    )
    return worker


async def resolve_route(
    db: AsyncSession, *, worker_id: UUID, route_stop_id: UUID | None, today: date
) -> Route:
    """The route to act against: the one owning the chosen stop on a re-submit, else the
    worker's started route for today."""
    if route_stop_id is not None:
        stop = await db.get(RouteStop, route_stop_id)
        if stop is None:
            raise RouteStopMissing(str(route_stop_id))
        route = await db.get(Route, stop.route_id)
        assert route is not None
        return route

    # IN_PROGRESS first; DONE as the fallback so a replay of the last check-out (offline sync
    # resending after a lost response) still reaches its idempotency check once that check-out
    # closed the route (RNF06). PAUSED too: a scan synced after the release paused the route
    # must reach the service and get its 409 STOP_RELEASED, not "no active route".
    started_route: Route | None = await db.scalar(
        select(Route)
        .where(
            Route.field_worker_id == worker_id,
            Route.route_date == today,
            Route.status.in_([RouteStatus.IN_PROGRESS, RouteStatus.PAUSED, RouteStatus.DONE]),
        )
        .order_by((Route.status == RouteStatus.IN_PROGRESS).desc(), Route.created_at.desc())
        .limit(1)
    )
    if started_route is None:
        raise NoActiveRoute
    return started_route


def stop_released_conflict(exc: StopReleased) -> HTTPException:
    """The 409 a check-in, check-out or evidence upload synced after its stop was released gets
    (spec 2026-10-20-alocacao-automatica §7.5): the app opens the pre-filled appeal when
    `appealable`.

        except StopReleased as exc:
            raise stop_released_conflict(exc) from exc
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "STOP_RELEASED",
            "detail": str(exc),
            "route_stop_id": str(exc.route_stop_id),
            "release_reason": exc.release_reason.value,
            "appealable": exc.appealable,
        },
    )
