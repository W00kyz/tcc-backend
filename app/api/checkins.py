"""RF29 — check-in via a real signed floor QR cross-checked with GPS.

The QR identifies a *floor*; the `check_in` service resolves which room on that floor from
the worker's PENDING stops there (spec §4.4) and attaches the anti-fraud flags without
blocking a reviewable check-in. When two or more rooms match, this endpoint answers 409
with the candidate list and the app re-submits with the chosen `route_stop_id`.

`check_in` owns and commits its own transaction (the documented service-layer exception),
so this router neither opens one nor records an audit trail for check-in."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._execution_common import (
    NoActiveRoute,
    RouteStopMissing,
    current_worker,
    resolve_route,
)
from app.api.deps import require_role
from app.db.session import get_db
from app.domain.execution.models import QrScan, QrScanKind
from app.domain.execution.service import (
    AmbiguousRoom,
    FloorNotOnRoute,
    QrCodeUnknown,
    QrRevoked,
    QrSignatureInvalid,
    RouteCancelled,
    RouteNotStarted,
    StopNotAssignedToWorker,
    check_in,
)
from app.domain.execution.validation import ChosenStopNotOnFloor
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/check-ins", tags=["execution"])

_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    QrSignatureInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrCodeUnknown: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrRevoked: status.HTTP_422_UNPROCESSABLE_ENTITY,
    FloorNotOnRoute: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ChosenStopNotOnFloor: status.HTTP_422_UNPROCESSABLE_ENTITY,
    StopNotAssignedToWorker: status.HTTP_403_FORBIDDEN,
    RouteNotStarted: status.HTTP_409_CONFLICT,
    RouteCancelled: status.HTTP_409_CONFLICT,
}


class CheckInRequest(BaseModel):
    qr_payload: str
    latitude: float | None = None
    longitude: float | None = None
    scanned_at: datetime
    idempotency_key: UUID
    route_stop_id: UUID | None = None  # the worker's room choice on a re-submit
    execution_id: UUID | None = None  # the offline app owns the id so a retry re-uses the row
    client_clock_offset_seconds: float | None = None  # server minus device clock (spec Ruling 7)


class CandidateOut(BaseModel):
    route_stop_id: UUID
    service_point_id: UUID
    name: str
    distance_m: float | None


class CheckInResponse(BaseModel):
    execution_id: UUID
    route_stop_id: UUID
    geo_validation: str
    validation_flags: list[str]
    review_status: str


@router.post("", response_model=CheckInResponse, status_code=status.HTTP_201_CREATED)
async def create_check_in(
    body: CheckInRequest,
    request: Request,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CheckInResponse:
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
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No active route for today; start your route first."
        ) from exc

    settings = request.app.state.settings
    public_key_hex = derive_public_key_hex(settings.qr_signing_private_key_hex)
    radius_m = (await get_settings_row(db)).check_radius_meters

    try:
        execution = await check_in(
            db,
            route=route,
            worker_id=worker.id,
            qr_payload=body.qr_payload,
            public_key_hex=public_key_hex,
            latitude=body.latitude,
            longitude=body.longitude,
            scanned_at=body.scanned_at,
            idempotency_key=body.idempotency_key,
            chosen_route_stop_id=body.route_stop_id,
            radius_m=radius_m,
            execution_id=body.execution_id,
            client_clock_offset_seconds=body.client_clock_offset_seconds,
        )
    except AmbiguousRoom as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "detail": "Multiple service points on this floor match; choose one.",
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
        ) from exc
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc

    scan = await db.scalar(
        select(QrScan).where(
            QrScan.execution_id == execution.id, QrScan.kind == QrScanKind.CHECK_IN
        )
    )
    assert scan is not None
    return CheckInResponse(
        execution_id=execution.id,
        route_stop_id=execution.route_stop_id,
        geo_validation=scan.geo_validation.value,
        validation_flags=execution.validation_flags,
        review_status=execution.review_status.value,
    )
