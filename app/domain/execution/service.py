"""Route start (RF34) and check-in (RF29) business logic. Only layer 1 of the anti-fraud
defense (QR signature) is enforced here — layers 2-5 (GPS radius, time window, QR status)
arrive in Etapas 3 and 5. See "Decisões de escopo desta etapa", item 6."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import ServicePoint
from app.domain.execution.models import Execution, ExecutionSource, GeoValidation, QrScan
from app.domain.qr.crypto import decode_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    StopAssignment,
    StopAssignmentOutcome,
)


class RouteAlreadyStarted(Exception):
    pass


class RouteNotStartable(Exception):
    """The route is CANCELLED (or DONE) — a worker cannot start it (spec §3 Ruling 4)."""


class QrSignatureInvalid(Exception):
    pass


class QrCodeUnknown(Exception):
    pass


class FloorMismatch(Exception):
    pass


class StopNotAssignedToWorker(Exception):
    pass


class RouteNotStarted(Exception):
    pass


class StopAlreadyDone(Exception):
    pass


async def start_route(
    db: AsyncSession, *, route: Route, latitude: float, longitude: float, started_at: datetime
) -> Route:
    if route.started_at is not None:
        raise RouteAlreadyStarted(f'Route "{route.id}" was already started at {route.started_at}.')
    if route.status != RouteStatus.PLANNED:
        # PLANNED is the only startable state — CANCELLED/DONE routes never move to IN_PROGRESS
        # (spec §3 Ruling 4). IN_PROGRESS is already ruled out by the started_at guard above.
        raise RouteNotStartable(
            f'Route "{route.id}" is {route.status.value} and cannot be started; expected PLANNED.'
        )

    route.started_at = started_at
    route.start_latitude = latitude
    route.start_longitude = longitude
    route.status = RouteStatus.IN_PROGRESS
    await db.commit()
    return route


async def check_in(
    db: AsyncSession,
    *,
    stop: RouteStop,
    worker_id: UUID,
    qr_payload: str,
    public_key_hex: str,
    latitude: float,
    longitude: float,
    scanned_at: datetime,
    idempotency_key: UUID,
) -> Execution:
    existing = await db.scalar(
        select(Execution).where(Execution.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    route = await db.get(Route, stop.route_id)
    # FK guarantees this — if missing, it's a bug elsewhere, not a case to handle here.
    assert route is not None

    if route.field_worker_id != worker_id:
        raise StopNotAssignedToWorker(
            f'Route stop "{stop.id}" is not on worker "{worker_id}"\'s route.'
        )
    if route.started_at is None:
        raise RouteNotStarted(f'Route "{route.id}" has not been started yet.')
    if stop.status == RouteStopStatus.DONE:
        raise StopAlreadyDone(f'Route stop "{stop.id}" was already checked in.')

    decoded = decode_qr_payload(qr_payload, public_key_hex=public_key_hex)
    if decoded is None:
        raise QrSignatureInvalid("QR signature failed verification.")

    qr_code = await db.scalar(select(QrCode).where(QrCode.public_code == qr_payload))
    if qr_code is None:
        raise QrCodeUnknown("QR payload does not match any registered code.")

    point = await db.get(ServicePoint, stop.service_point_id)
    assert point is not None
    if decoded.floor_id != point.floor_id:
        raise FloorMismatch(
            f'QR is for floor "{decoded.floor_id}", but stop "{stop.id}" is on floor '
            f'"{point.floor_id}".'
        )

    now = datetime.now(UTC)
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker_id,
        checked_in_at=scanned_at,
        synced_at=now,
        source=ExecutionSource.APP,
        idempotency_key=idempotency_key,
    )
    db.add(execution)
    await db.flush()

    db.add(
        QrScan(
            execution_id=execution.id,
            qr_code_id=qr_code.id,
            scanned_at=scanned_at,
            received_at=now,
            geo_validation=GeoValidation.NOT_VALIDATED,
            latitude=latitude,
            longitude=longitude,
        )
    )
    stop.status = RouteStopStatus.DONE

    assignment = await db.scalar(
        select(StopAssignment)
        .where(StopAssignment.route_stop_id == stop.id)
        .order_by(StopAssignment.sequence.desc())
        .limit(1)
    )
    if assignment is not None:
        assignment.outcome = StopAssignmentOutcome.EXECUTED

    await db.commit()
    return execution
