"""RF29 (check-in via real QR cross-checked with GPS).

NOTE: this endpoint is a thin bridge to the Etapa 5 floor-first `check_in` service — the
full rewrite (structured 409 candidate list, optional GPS, `SystemSettings` radius) lands
in Task 5. It keeps the request contract stable and forwards the worker's scanned stop as
the room choice so the existing app flow still works."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.catalog.models import FieldWorker
from app.domain.execution.service import (
    AmbiguousRoom,
    FloorMismatch,
    FloorNotOnRoute,
    QrCodeUnknown,
    QrRevoked,
    QrSignatureInvalid,
    RouteCancelled,
    RouteNotStarted,
    StopAlreadyDone,
    StopNotAssignedToWorker,
    check_in,
)
from app.domain.execution.validation import ChosenStopNotOnFloor
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex
from app.domain.routing.models import Route, RouteStop
from app.domain.settings.models import SystemSettings

router = APIRouter(prefix="/check-ins", tags=["execution"])

_DOMAIN_ERROR_STATUS: dict[type[Exception], int] = {
    QrSignatureInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrCodeUnknown: status.HTTP_422_UNPROCESSABLE_ENTITY,
    QrRevoked: status.HTTP_422_UNPROCESSABLE_ENTITY,
    FloorMismatch: status.HTTP_422_UNPROCESSABLE_ENTITY,
    FloorNotOnRoute: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ChosenStopNotOnFloor: status.HTTP_422_UNPROCESSABLE_ENTITY,
    AmbiguousRoom: status.HTTP_409_CONFLICT,
    StopNotAssignedToWorker: status.HTTP_403_FORBIDDEN,
    RouteNotStarted: status.HTTP_409_CONFLICT,
    RouteCancelled: status.HTTP_409_CONFLICT,
    StopAlreadyDone: status.HTTP_409_CONFLICT,
}


class CheckInRequest(BaseModel):
    route_stop_id: UUID
    qr_payload: str
    latitude: float
    longitude: float
    scanned_at: datetime
    idempotency_key: UUID


class CheckInResponse(BaseModel):
    execution_id: UUID
    checked_in_at: datetime


@router.post("", response_model=CheckInResponse, status_code=status.HTTP_201_CREATED)
async def create_check_in(
    body: CheckInRequest,
    request: Request,
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CheckInResponse:
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no field worker profile.")

    stop = await db.get(RouteStop, body.route_stop_id)
    if stop is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Route stop "{body.route_stop_id}" not found.'
        )
    route = await db.get(Route, stop.route_id)
    assert route is not None

    settings = request.app.state.settings
    public_key_hex = derive_public_key_hex(settings.qr_signing_private_key_hex)
    radius_row = await db.scalar(select(SystemSettings))
    radius_m = radius_row.check_radius_meters if radius_row is not None else 50

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
        )
    except tuple(_DOMAIN_ERROR_STATUS) as exc:
        raise HTTPException(_DOMAIN_ERROR_STATUS[type(exc)], str(exc)) from exc

    return CheckInResponse(execution_id=execution.id, checked_in_at=execution.checked_in_at)
