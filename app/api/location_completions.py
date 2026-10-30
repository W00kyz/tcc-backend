"""Completion of a stop at an outdoor place by the worker's position (spec
2026-10-30-locais-externos §4), the counterpart of the floor-QR check-in/check-out.

`complete_by_location` owns and commits its transaction (the documented service-layer
exception), so this router neither opens one nor records an audit trail. It only maps the
domain's refusals to HTTP; `_refusal` is the one place they pass through."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._execution_common import (
    NoActiveRoute,
    RouteStopMissing,
    resolve_route,
    stop_released_conflict,
)
from app.api.deps import ApprovedWorker, CampusToday, Now
from app.db.session import get_db
from app.domain.execution.location_completion import (
    LocationRequired,
    NotALocationStop,
    OutOfRadius,
    StopAlreadyDone,
    complete_by_location,
)
from app.domain.execution.service import RouteNotStarted, StopNotAssignedToWorker, StopReleased
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/location-completions", tags=["execution"])


class LocationCompletionRequest(BaseModel):
    route_stop_id: UUID
    execution_id: UUID  # the offline app owns the id: evidence is uploaded against it later
    idempotency_key: UUID
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # The fix's reported accuracy; accepted so the app can send it, not used by the radius rule.
    accuracy_m: float | None = Field(default=None, ge=0)
    completed_at: datetime  # the device clock, as `scanned_at` is for check-in
    client_clock_offset_seconds: float | None = None  # server minus device clock (spec Ruling 7)
    # Free text typed at completion; blank is stored as null, over 2000 characters is a 422.
    complaints_suggestions: str | None = Field(default=None, max_length=2000)


class LocationCompletionResponse(BaseModel):
    execution_id: UUID
    route_stop_id: UUID
    completed_at: datetime
    validation_flags: list[str]
    review_status: str


def _refusal(exc: Exception) -> HTTPException | None:
    """The HTTP answer for a domain refusal, `None` for any other exception."""
    match exc:
        case LocationRequired():
            return _unprocessable("LOCATION_REQUIRED", str(exc))
        case OutOfRadius():
            return _unprocessable(
                "OUT_OF_RADIUS", str(exc), distance_m=exc.distance_m, radius_m=exc.radius_m
            )
        case NotALocationStop():
            return _unprocessable("NOT_A_LOCATION_STOP", str(exc))
        case StopReleased():
            return stop_released_conflict(exc)
        case StopAlreadyDone() | RouteNotStarted():
            return HTTPException(status.HTTP_409_CONFLICT, str(exc))
        case StopNotAssignedToWorker():
            return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return None


def _unprocessable(code: str, detail: str, **extra: float) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": code, "detail": detail, **extra}
    )


@router.post("", response_model=LocationCompletionResponse, status_code=status.HTTP_201_CREATED)
async def create_location_completion(
    body: LocationCompletionRequest,
    worker: ApprovedWorker,
    db: Annotated[AsyncSession, Depends(get_db)],
    today: CampusToday,
    now: Now,
) -> LocationCompletionResponse:
    try:
        route = await resolve_route(
            db, worker_id=worker.id, route_stop_id=body.route_stop_id, today=today
        )
    except RouteStopMissing as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Route stop "{body.route_stop_id}" not found.'
        ) from exc
    except NoActiveRoute as exc:  # unreachable with a stop id; keeps the mapping total
        raise HTTPException(status.HTTP_409_CONFLICT, "No active route for today.") from exc

    radius_m = (await get_settings_row(db)).check_radius_meters
    try:
        execution = await complete_by_location(
            db,
            route=route,
            worker_id=worker.id,
            route_stop_id=body.route_stop_id,
            execution_id=body.execution_id,
            idempotency_key=body.idempotency_key,
            latitude=body.latitude,
            longitude=body.longitude,
            completed_at=body.completed_at,
            client_clock_offset_seconds=body.client_clock_offset_seconds,
            complaints_suggestions=body.complaints_suggestions,
            radius_m=radius_m,
            now=now,
        )
    except (
        LocationRequired,
        OutOfRadius,
        NotALocationStop,
        StopReleased,
        StopAlreadyDone,
        RouteNotStarted,
        StopNotAssignedToWorker,
    ) as exc:
        # Every refusal passes here: the one place to record an alert before answering.
        refusal = _refusal(exc)
        assert refusal is not None
        raise refusal from exc

    assert execution.checked_out_at is not None
    return LocationCompletionResponse(
        execution_id=execution.id,
        route_stop_id=execution.route_stop_id,
        completed_at=execution.checked_out_at,
        validation_flags=execution.validation_flags,
        review_status=execution.review_status.value,
    )
