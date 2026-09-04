"""RF31 — check-out via a real signed floor QR that closes the open check-in on that floor.

The mirror of `POST /check-ins`: the QR identifies a *floor*; the `check_out` service
resolves which open check-in on that floor to close (spec §4.4), merges any new anti-fraud
flags into the execution, and marks the stop DONE. When two or more open check-ins match,
this endpoint answers 409 with the candidate list and the app re-submits with the chosen
`route_stop_id`.

`check_out` owns and commits its own transaction (the documented service-layer exception),
so this router neither opens one nor records an audit trail for check-out."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._execution_common import (
    NoActiveRoute,
    RouteStopMissing,
    current_worker,
    resolve_route,
)
from app.api.checkins import CandidateOut
from app.api.deps import require_role
from app.db.session import get_db
from app.domain.execution.answers import AnswerIn, AnswerValidationError
from app.domain.execution.service import (
    AmbiguousRoom,
    FormVersionMismatch,
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
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/check-outs", tags=["execution"])

_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    QrSignatureInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrCodeUnknown: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrRevoked: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ChosenStopNotOnFloor: status.HTTP_422_UNPROCESSABLE_ENTITY,
    NoOpenCheckIn: status.HTTP_422_UNPROCESSABLE_ENTITY,
    AnswerValidationError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    FormVersionMismatch: status.HTTP_422_UNPROCESSABLE_ENTITY,
    StopNotAssignedToWorker: status.HTTP_403_FORBIDDEN,
    RouteCancelled: status.HTTP_409_CONFLICT,
}


class AnswerInBody(BaseModel):
    stable_key: str
    value: Any


class CheckOutRequest(BaseModel):
    qr_payload: str
    latitude: float | None = None
    longitude: float | None = None
    scanned_at: datetime
    checkout_idempotency_key: UUID
    route_stop_id: UUID | None = None  # the worker's room choice on a re-submit
    execution_id: UUID | None = None  # accepted for symmetry with check-in; ignored by check-out
    client_clock_offset_seconds: float | None = None  # server minus device clock (spec Ruling 7)
    # Etapa 7 (Ruling 5): the execution form travels with the check-out — both or neither.
    form_version_id: UUID | None = None
    answers: list[AnswerInBody] | None = None


class CheckOutResponse(BaseModel):
    execution_id: UUID
    route_stop_id: UUID
    checked_out_at: datetime
    validation_flags: list[str]
    review_status: str


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
    worker = await current_worker(db, user)
    if worker is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no field worker profile.")

    try:
        route = await resolve_route(db, worker_id=worker.id, route_stop_id=body.route_stop_id)
    except RouteStopMissing as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Route stop "{body.route_stop_id}" not found.'
        ) from exc
    except NoActiveRoute as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "No active route for today.") from exc

    settings = request.app.state.settings
    public_key_hex = derive_public_key_hex(settings.qr_signing_private_key_hex)
    radius_m = (await get_settings_row(db)).check_radius_meters

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
            execution_id=body.execution_id,
            client_clock_offset_seconds=body.client_clock_offset_seconds,
            form_version_id=body.form_version_id,
            answers=(
                [AnswerIn(stable_key=a.stable_key, value=a.value) for a in body.answers]
                if body.answers is not None
                else None
            ),
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
