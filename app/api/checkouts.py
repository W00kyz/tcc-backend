"""RF31 — check-out via a real signed floor QR that closes the open check-in on that floor.

The mirror of `POST /check-ins`: the QR identifies a *floor*; the `check_out` service
resolves which open check-in on that floor to close (spec §4.4), merges any new anti-fraud
flags into the execution, and marks the stop DONE. When two or more open check-ins match,
this endpoint answers 409 with the candidate list and the app re-submits with the chosen
`route_stop_id`.

`check_out` owns and commits its own transaction (the documented service-layer exception),
so this router neither opens one nor records an audit trail for check-out."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.checkins import CandidateOut
from app.api.deps import require_role
from app.db.session import get_db
from app.domain.catalog.models import FieldWorker
from app.domain.execution.service import (
    AmbiguousRoom,
    NoOpenCheckIn,
    QrCodeUnknown,
    QrRevoked,
    QrSignatureInvalid,
    RouteCancelled,
    StopNotAssignedToWorker,
    check_out,
)
from app.domain.execution.validation import ChosenStopNotOnFloor
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex
from app.domain.routing.models import Route, RouteStatus, RouteStop
from app.domain.settings.models import SystemSettings

router = APIRouter(prefix="/check-outs", tags=["execution"])

# Fallback when the singleton settings row is somehow absent (the Etapa 5 migration seeds
# it); Task 11 extracts `get_settings_row` and this inline read goes with it.
_DEFAULT_RADIUS_M = 50

_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    QrSignatureInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrCodeUnknown: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrRevoked: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ChosenStopNotOnFloor: status.HTTP_422_UNPROCESSABLE_ENTITY,
    NoOpenCheckIn: status.HTTP_422_UNPROCESSABLE_ENTITY,
    StopNotAssignedToWorker: status.HTTP_403_FORBIDDEN,
    RouteCancelled: status.HTTP_409_CONFLICT,
}


class CheckOutRequest(BaseModel):
    qr_payload: str
    latitude: float | None = None
    longitude: float | None = None
    scanned_at: datetime
    checkout_idempotency_key: UUID
    route_stop_id: UUID | None = None  # the worker's room choice on a re-submit


class CheckOutResponse(BaseModel):
    execution_id: UUID
    route_stop_id: UUID
    checked_out_at: datetime
    validation_flags: list[str]
    review_status: str


async def _resolve_route(db: AsyncSession, body: CheckOutRequest, worker_id: UUID) -> Route:
    """The route to check out against: the one owning the chosen stop on a re-submit, else the
    worker's started route for today."""
    if body.route_stop_id is not None:
        stop = await db.get(RouteStop, body.route_stop_id)
        if stop is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f'Route stop "{body.route_stop_id}" not found.'
            )
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
        raise HTTPException(status.HTTP_409_CONFLICT, "No active route for today.")
    return started_route


def _ambiguous_conflict(exc: AmbiguousRoom) -> HTTPException:
    """The 409 body the app re-submits against — identical shape to check-in's."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "detail": "Multiple open check-ins on this floor match; choose one.",
            "candidates": [
                CandidateOut(
                    route_stop_id=c.route_stop_id,
                    service_point_id=c.service_point_id,
                    name=c.name,
                    distance_m=c.distance_m,
                ).model_dump(mode="json")
                for c in exc.candidates
            ],
        },
    )


@router.post("", response_model=CheckOutResponse, status_code=status.HTTP_201_CREATED)
async def create_check_out(
    body: CheckOutRequest,
    request: Request,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CheckOutResponse:
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no field worker profile.")

    route = await _resolve_route(db, body, worker.id)

    settings = request.app.state.settings
    public_key_hex = derive_public_key_hex(settings.qr_signing_private_key_hex)
    settings_row = await db.scalar(select(SystemSettings))
    radius_m = settings_row.check_radius_meters if settings_row is not None else _DEFAULT_RADIUS_M

    try:
        execution = await check_out(
            db,
            route=route,
            worker_id=worker.id,
            qr_payload=body.qr_payload,
            public_key_hex=public_key_hex,
            latitude=body.latitude,
            longitude=body.longitude,
            scanned_at=body.scanned_at,
            checkout_idempotency_key=body.checkout_idempotency_key,
            chosen_route_stop_id=body.route_stop_id,
            radius_m=radius_m,
        )
    except AmbiguousRoom as exc:
        raise _ambiguous_conflict(exc) from exc
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc

    assert execution.checked_out_at is not None
    return CheckOutResponse(
        execution_id=execution.id,
        route_stop_id=execution.route_stop_id,
        checked_out_at=execution.checked_out_at,
        validation_flags=execution.validation_flags,
        review_status=execution.review_status.value,
    )
