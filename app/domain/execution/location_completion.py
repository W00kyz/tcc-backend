"""Completion by location (spec 2026-10-30-locais-externos §4): a stop at an outdoor place has no
floor QR to scan, so the worker finishes it with one call carrying the device position. The
server accepts it when the fix is within `check_radius_meters` of the point; otherwise it refuses
and the stop stays open — a refused completion is never written as done.

DO NOT CHANGE: like check-in and check-out (`service.py`), `complete_by_location` owns and
commits its transaction. The refusals are plain exceptions; the router maps them to HTTP."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import Floor, ServicePoint
from app.domain.catalog.sites import site_of
from app.domain.execution.evidence_service import count_execution_photos
from app.domain.execution.geo import haversine_meters
from app.domain.execution.models import Execution, ExecutionReviewStatus, ExecutionSource
from app.domain.execution.service import (
    RouteNotStarted,
    StopNotAssignedToWorker,
    released_stop_error,
)
from app.domain.execution.validation import (
    FLAG_INSUFFICIENT_EVIDENCE,
    MIN_EVIDENCE_PHOTOS,
    clock_skew_flag,
)
from app.domain.routing.models import Route, RouteStop, RouteStopStatus


class LocationRequired(Exception):  # noqa: N818 — a refusal condition, mapped by name in the API
    """The completion carried no position (no GPS fix): there is nothing to check against the
    place, so the stop is refused and stays open."""


class OutOfRadius(Exception):  # noqa: N818 — a refusal condition, mapped by name in the API
    """The fix is farther from the point than `check_radius_meters`."""

    def __init__(self, distance_m: float, radius_m: int) -> None:
        super().__init__(
            f"The position is {distance_m:.1f} m from the place; "
            f"expected at most {radius_m} m (the check radius)."
        )
        self.distance_m = distance_m
        self.radius_m = radius_m


class NotALocationStop(Exception):  # noqa: N818 — a refusal condition, mapped by name in the API
    """The stop is not one this call completes: its point is in a building (finished by the
    floor QR) or the stop is not PENDING / IN_PROGRESS."""


class StopAlreadyDone(Exception):  # noqa: N818 — a refusal condition, mapped by name in the API
    """The stop was already completed by another execution. The app's sync treats this answer
    as applied, so the message keeps the word "already"."""

    def __init__(self, route_stop_id: uuid.UUID) -> None:
        super().__init__(f'Route stop "{route_stop_id}" is already done.')
        self.route_stop_id = route_stop_id


async def complete_by_location(
    db: AsyncSession,
    *,
    route: Route,
    worker_id: uuid.UUID,
    route_stop_id: uuid.UUID,
    execution_id: uuid.UUID,
    idempotency_key: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
    completed_at: datetime,
    client_clock_offset_seconds: float | None,
    complaints_suggestions: str | None,
    radius_m: int,
    now: datetime,
) -> Execution:
    """Finish a place stop: one execution with `checked_in_at == checked_out_at == completed_at`,
    no QR scan, the stop DONE. A replay of `idempotency_key` returns the first execution.
    Missing evidence (`requires_evidence` with fewer than `MIN_EVIDENCE_PHOTOS` photos) and a
    skewed device clock flag the execution for review, never refuse it; the photos arrive later
    through the evidence endpoint, keyed by `execution_id`. `now` is the server clock.

        execution = await complete_by_location(db, route=route, worker_id=worker.id, ...)
    """
    existing = await db.scalar(
        select(Execution).where(Execution.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    _guard_worker_route(route, worker_id)
    stop, point = await _load_location_stop(db, route=route, route_stop_id=route_stop_id)
    await _guard_stop_state(db, stop, point, now=now)
    distance_m = _distance_to_place(point, latitude, longitude)
    if distance_m > radius_m:
        raise OutOfRadius(distance_m, radius_m)

    skew_flags = clock_skew_flag(client_clock_offset_seconds)
    flags = sorted({*skew_flags, *await _evidence_flags(db, stop, execution_id)})
    execution = Execution(
        # The offline app owns the id so evidence uploaded later finds this row (spec Ruling 7).
        id=execution_id,
        route_stop_id=stop.id,
        field_worker_id=worker_id,
        checked_in_at=completed_at,
        checked_out_at=completed_at,
        synced_at=now,
        source=ExecutionSource.APP,
        idempotency_key=idempotency_key,
        review_status=ExecutionReviewStatus.PENDING_REVIEW if flags else ExecutionReviewStatus.NONE,
        validation_flags=flags,
        clock_skew_seconds=client_clock_offset_seconds if skew_flags else None,
        complaints_suggestions=(complaints_suggestions or "").strip() or None,
    )
    db.add(execution)
    stop.status = RouteStopStatus.DONE
    await db.commit()
    return execution


# --- internal helpers -----------------------------------------------------------------------


def _guard_worker_route(route: Route, worker_id: uuid.UUID) -> None:
    """Same answers as the QR flows: another worker's route is refused, an unstarted one waits."""
    if route.field_worker_id != worker_id:
        raise StopNotAssignedToWorker(f'Route "{route.id}" is not on worker "{worker_id}"\'s list.')
    if route.started_at is None:
        raise RouteNotStarted(f'Route "{route.id}" has not been started yet.')


async def _load_location_stop(
    db: AsyncSession, *, route: Route, route_stop_id: uuid.UUID
) -> tuple[RouteStop, ServicePoint]:
    """The locked stop and its point (place and floor loaded for `site_of`)."""
    stop = await db.scalar(
        select(RouteStop)
        .where(RouteStop.id == route_stop_id, RouteStop.route_id == route.id)
        # IMPORTANT: lock the stop until the commit, as `_load_pending_candidates` does for
        # check-in: a racing release either waits and spares the stop or wins, and then this
        # read sees RELEASED — never a RELEASED stop flipped to DONE. `populate_existing`
        # because the caller's session may hold a stale copy.
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stop is None:
        raise StopNotAssignedToWorker(
            f'Route stop "{route_stop_id}" is not on route "{route.id}"; expected one of its stops.'
        )
    point = await db.scalar(
        select(ServicePoint)
        .where(ServicePoint.id == stop.service_point_id)
        .options(selectinload(ServicePoint.place))
        .options(selectinload(ServicePoint.floor).selectinload(Floor.building))
    )
    assert point is not None  # the foreign key guarantees it
    return stop, point


async def _guard_stop_state(
    db: AsyncSession, stop: RouteStop, point: ServicePoint, *, now: datetime
) -> None:
    if site_of(point).kind != "PLACE":
        raise NotALocationStop(
            f'Route stop "{stop.id}" is in a building; expected a stop at an outdoor place '
            "(a building stop is finished with the floor QR)."
        )
    if stop.status is RouteStopStatus.RELEASED:
        raise await released_stop_error(db, stop=stop, now=now)
    if stop.status is RouteStopStatus.DONE:
        raise StopAlreadyDone(stop.id)


def _distance_to_place(
    point: ServicePoint, latitude: float | None, longitude: float | None
) -> float:
    """Metres from the fix to the point, by the same haversine the QR flows use."""
    if latitude is None or longitude is None:
        raise LocationRequired(
            "The completion carried no position; expected latitude and longitude "
            "from the device to check it against the place."
        )
    return haversine_meters(latitude, longitude, point.latitude, point.longitude)


async def _evidence_flags(db: AsyncSession, stop: RouteStop, execution_id: uuid.UUID) -> list[str]:
    if not stop.requires_evidence:
        return []
    if await count_execution_photos(db, execution_id) >= MIN_EVIDENCE_PHOTOS:
        return []
    return [FLAG_INSUFFICIENT_EVIDENCE]
