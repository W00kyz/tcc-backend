"""Check-in (RF29) and check-out (RF31). Check-in is floor-QR-first: the QR identifies a
floor, `resolve_room` picks the room from the worker's PENDING stops there, and the anti-fraud
layers (signature, QR status, GPS radius, room match, clock skew) attach flags without blocking
a reviewable check-in (spec §4.2, §4.4). The schedule-window layer went with the planned arrival
windows (spec 2026-10-20-alocacao-automatica §2.1).

DO NOT CHANGE: this module is the documented exception to "service layer flushes, never
commits" — check-in and check-out each own and commit a transaction."""

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.appeals.service import is_appealable, stop_has_appeal
from app.domain.catalog.footprint import outline_contains
from app.domain.catalog.models import Building, Floor, ServicePoint
from app.domain.execution.geo import haversine_meters
from app.domain.execution.models import (
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    GeoValidation,
    QrScan,
    QrScanKind,
)
from app.domain.execution.validation import (
    FLAG_QR_SUPERSEDED,
    Candidate,
    clock_skew_flag,
    resolve_room,
)
from app.domain.qr.crypto import QrPayload, decode_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.routing.models import ReleaseReason, Route, RouteStop, RouteStopStatus
from app.domain.settings.service import get_settings_row


class QrSignatureInvalid(Exception):
    pass


class QrCodeUnknown(Exception):
    pass


class QrRevoked(Exception):
    """The scanned QR code is REVOKED (anti-fraud layer 2)."""


class FloorMismatch(Exception):
    """LEGACY: the pre-Etapa-5 stop-first check-in raised this. Kept until Task 5 rewrites
    the API error map; the floor-first path raises `FloorNotOnRoute` instead."""


class FloorNotOnRoute(Exception):
    """The scanned QR's floor has no PENDING stop on this worker's route."""


class AmbiguousRoom(Exception):
    """Two or more service points on the scanned floor match — the app must pick one and
    re-submit with `chosen_route_stop_id` (spec §4.4)."""

    def __init__(self, candidates: list[Candidate]) -> None:
        super().__init__(f"{len(candidates)} rooms on this floor match; a choice is required.")
        self.candidates = candidates


class StopNotAssignedToWorker(Exception):
    pass


class RouteNotStarted(Exception):
    pass


class NoOpenCheckIn(Exception):
    """Check-out found no IN_PROGRESS stop with an open execution on the scanned floor."""


class StopReleased(Exception):
    """The stop went back to the pool before this check-in, check-out or evidence reached the
    server — a late offline sync (spec 2026-10-20-alocacao-automatica §7.5). Nothing is written;
    `appealable` tells the app whether to offer the worker an appeal."""

    def __init__(self, stop: RouteStop, *, appealable: bool) -> None:
        assert stop.release_reason is not None  # every RELEASED stop carries its reason
        super().__init__(
            f'Route stop "{stop.id}" was released back to the pool ({stop.release_reason}).'
        )
        self.route_stop_id = stop.id
        self.release_reason: ReleaseReason = stop.release_reason
        self.appealable = appealable


async def check_in(
    db: AsyncSession,
    *,
    route: Route,
    worker_id: uuid.UUID,
    qr_payload: str,
    public_key_hex: str,
    latitude: float | None,
    longitude: float | None,
    scanned_at: datetime,
    idempotency_key: uuid.UUID,
    chosen_route_stop_id: uuid.UUID | None,
    radius_m: int,
    now: datetime,
    execution_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
) -> Execution:
    """Floor-QR check-in: resolves the room, attaches anti-fraud flags, moves the stop to
    IN_PROGRESS. Pass `chosen_route_stop_id` on the re-submit after an `AmbiguousRoom`;
    `latitude`/`longitude` are `None` when the device has no GPS fix. The offline app supplies
    `execution_id` so a retried check-in re-uses the same row, and reports its measured
    `client_clock_offset_seconds` (server minus device clock) so implausible skew is flagged.
    `now` is the server clock: `synced_at`, the scan's `received_at`, the appeal window."""
    existing = await db.scalar(
        select(Execution).where(Execution.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    _guard_route(route, worker_id, require_started=True)
    decoded, qr_code, qr_flags = await _verify_qr(db, qr_payload, public_key_hex)

    candidates, stops = await _load_pending_candidates(
        db, route_id=route.id, floor_id=decoded.floor_id, latitude=latitude, longitude=longitude
    )
    if not candidates or _chose_another_stop(chosen_route_stop_id, stops):
        await _raise_if_released_on_floor(
            db,
            route=route,
            floor_id=decoded.floor_id,
            chosen_route_stop_id=chosen_route_stop_id,
            now=now,
        )
    if not candidates:
        raise FloorNotOnRoute(
            f'QR floor "{decoded.floor_id}" has no PENDING stop on route "{route.id}".'
        )

    has_gps = latitude is not None and longitude is not None
    resolution = resolve_room(
        candidates, radius_m=radius_m, has_gps=has_gps, chosen_route_stop_id=chosen_route_stop_id
    )
    if resolution.resolved is None:
        raise AmbiguousRoom(resolution.ambiguous)

    resolved = resolution.resolved
    stop = stops[resolved.route_stop_id]
    skew_flags = clock_skew_flag(client_clock_offset_seconds)
    flags = _merge_flags(resolution.flags, qr_flags, skew_flags)

    execution = Execution(
        # The offline app owns the id so a retry lands on the same row (spec Ruling 7).
        id=execution_id or uuid.uuid4(),
        route_stop_id=stop.id,
        field_worker_id=worker_id,
        checked_in_at=scanned_at,
        synced_at=now,
        source=ExecutionSource.APP,
        idempotency_key=idempotency_key,
        review_status=ExecutionReviewStatus.PENDING_REVIEW if flags else ExecutionReviewStatus.NONE,
        validation_flags=flags,
        clock_skew_seconds=client_clock_offset_seconds if skew_flags else None,
    )
    db.add(execution)
    await db.flush()

    db.add(
        _build_scan(
            execution_id=execution.id,
            qr_code_id=qr_code.id,
            kind=QrScanKind.CHECK_IN,
            scanned_at=scanned_at,
            received_at=now,
            resolved=resolved,
            geo_validation=resolution.geo_validation,
            latitude=latitude,
            longitude=longitude,
        )
    )
    stop.status = RouteStopStatus.IN_PROGRESS
    await db.commit()
    return execution


async def check_out(
    db: AsyncSession,
    *,
    route: Route,
    worker_id: uuid.UUID,
    qr_payload: str,
    public_key_hex: str,
    latitude: float | None,
    longitude: float | None,
    scanned_at: datetime,
    checkout_idempotency_key: uuid.UUID,
    chosen_route_stop_id: uuid.UUID | None,
    radius_m: int,
    now: datetime,
    execution_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
) -> Execution:
    """Close the open check-in on the scanned floor: sets `checked_out_at`, merges any new
    flags into the execution and marks the stop DONE. `execution_id`
    is accepted for API symmetry with check-in but ignored here — check-out resolves the open
    execution by floor, it does not create one. `client_clock_offset_seconds` still flags
    implausible skew. `now` is the server clock, as for the check-in."""
    _ = execution_id  # unused: check-out closes an existing execution, never creates one
    existing = await db.scalar(
        select(Execution).where(Execution.checkout_idempotency_key == checkout_idempotency_key)
    )
    if existing is not None:
        return existing

    _guard_route(route, worker_id, require_started=False)
    decoded, qr_code, qr_flags = await _verify_qr(db, qr_payload, public_key_hex)

    open_rows = await _load_open_checkins(
        db, route_id=route.id, floor_id=decoded.floor_id, latitude=latitude, longitude=longitude
    )
    open_stop_ids = {candidate.route_stop_id for candidate, _, _ in open_rows}
    if not open_rows or _chose_another_stop(chosen_route_stop_id, open_stop_ids):
        # The open check-in's stop was released — or, both scans offline, the stop was released
        # before either synced: the same answer as the check-in got.
        await _raise_if_released_on_floor(
            db,
            route=route,
            floor_id=decoded.floor_id,
            chosen_route_stop_id=chosen_route_stop_id,
            now=now,
        )
    if not open_rows:
        raise NoOpenCheckIn(
            f'Route "{route.id}" has no open check-in on floor "{decoded.floor_id}".'
        )

    has_gps = latitude is not None and longitude is not None
    candidates = [candidate for candidate, _, _ in open_rows]
    resolution = resolve_room(
        candidates, radius_m=radius_m, has_gps=has_gps, chosen_route_stop_id=chosen_route_stop_id
    )
    if resolution.resolved is None:
        raise AmbiguousRoom(resolution.ambiguous)

    resolved = resolution.resolved
    execution, stop = next(
        (execution, stop)
        for candidate, execution, stop in open_rows
        if candidate.route_stop_id == resolved.route_stop_id
    )

    previous_flags = set(execution.validation_flags)
    skew_flags = clock_skew_flag(client_clock_offset_seconds)
    checkout_flags = _merge_flags(resolution.flags, qr_flags, skew_flags)
    if skew_flags and execution.clock_skew_seconds is None:
        execution.clock_skew_seconds = client_clock_offset_seconds
    merged = previous_flags | set(checkout_flags)
    execution.validation_flags = sorted(merged)
    if merged - previous_flags:
        # A check-out-time anomaly must reach a human even if a manager already resolved the
        # check-in review — the resolve covered a different scan (spec review-queue owner ruling).
        execution.review_status = ExecutionReviewStatus.PENDING_REVIEW
    elif merged and execution.review_status is ExecutionReviewStatus.NONE:
        execution.review_status = ExecutionReviewStatus.PENDING_REVIEW
    execution.checked_out_at = scanned_at
    execution.checkout_idempotency_key = checkout_idempotency_key

    db.add(
        _build_scan(
            execution_id=execution.id,
            qr_code_id=qr_code.id,
            kind=QrScanKind.CHECK_OUT,
            scanned_at=scanned_at,
            received_at=now,
            resolved=resolved,
            geo_validation=resolution.geo_validation,
            latitude=latitude,
            longitude=longitude,
        )
    )
    stop.status = RouteStopStatus.DONE
    await db.commit()
    return execution


async def released_stop_error(db: AsyncSession, *, stop: RouteStop, now: datetime) -> StopReleased:
    """The `StopReleased` for a RELEASED `stop`, with whether its owner may still appeal it —
    the same predicate `POST /appeals` applies (spec §7.5).

        raise await released_stop_error(db, stop=stop, now=now)
    """
    appealable = is_appealable(
        stop,
        now=now,
        window_hours=(await get_settings_row(db)).appeal_window_hours,
        has_appeal=await stop_has_appeal(db, stop.id),
    )
    return StopReleased(stop, appealable=appealable)


async def ensure_stop_not_released(
    db: AsyncSession, *, execution: Execution, now: datetime
) -> None:
    """Raise `StopReleased` when `execution`'s stop went back to the pool — evidence synced
    after the release is refused like the late check-out it belongs to.

        await ensure_stop_not_released(db, execution=execution, now=now)
    """
    stop = await db.get(RouteStop, execution.route_stop_id)
    if stop is not None and stop.status is RouteStopStatus.RELEASED:
        raise await released_stop_error(db, stop=stop, now=now)


# --- internal helpers -----------------------------------------------------------------------


async def _raise_if_released_on_floor(
    db: AsyncSession,
    *,
    route: Route,
    floor_id: uuid.UUID,
    chosen_route_stop_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Raise `StopReleased` when a stop of `route` on the scanned floor was released: the
    worker's chosen stop if they chose one, else the latest released there."""
    released = list(
        await db.scalars(
            select(RouteStop)
            .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
            .where(
                RouteStop.route_id == route.id,
                RouteStop.status == RouteStopStatus.RELEASED,
                ServicePoint.floor_id == floor_id,
            )
            .order_by(RouteStop.released_at.desc())
        )
    )
    if chosen_route_stop_id is not None:
        released = [stop for stop in released if stop.id == chosen_route_stop_id]
    if released:
        raise await released_stop_error(db, stop=released[0], now=now)


def _chose_another_stop(
    chosen_route_stop_id: uuid.UUID | None, live_stop_ids: Collection[uuid.UUID]
) -> bool:
    """The worker's re-submit names a stop that is not among the live ones on the floor."""
    return chosen_route_stop_id is not None and chosen_route_stop_id not in live_stop_ids


def _guard_route(route: Route, worker_id: uuid.UUID, *, require_started: bool) -> None:
    if route.field_worker_id != worker_id:
        raise StopNotAssignedToWorker(f'Route "{route.id}" is not on worker "{worker_id}"\'s list.')
    if require_started and route.started_at is None:
        raise RouteNotStarted(f'Route "{route.id}" has not been started yet.')


async def _verify_qr(
    db: AsyncSession, qr_payload: str, public_key_hex: str
) -> tuple[QrPayload, QrCode, list[str]]:
    decoded = decode_qr_payload(qr_payload, public_key_hex=public_key_hex)
    if decoded is None:
        raise QrSignatureInvalid("QR signature failed verification.")

    qr_code = await db.scalar(select(QrCode).where(QrCode.public_code == qr_payload))
    if qr_code is None:
        raise QrCodeUnknown(f'QR payload "{qr_payload}" does not match any registered code.')
    if qr_code.status is QrCodeStatus.REVOKED:
        raise QrRevoked(f'QR code "{qr_code.id}" was revoked and cannot be used.')

    newer = await db.scalar(
        select(QrCode).where(
            QrCode.floor_id == decoded.floor_id,
            QrCode.status == QrCodeStatus.ACTIVE,
            QrCode.version > qr_code.version,
        )
    )
    qr_flags = [FLAG_QR_SUPERSEDED] if newer is not None else []
    return decoded, qr_code, qr_flags


def _distance_to_point(
    point: ServicePoint,
    footprint: dict[str, Any] | None,
    latitude: float | None,
    longitude: float | None,
) -> float | None:
    """Metres from the fix to the point; 0 when the fix is inside the building outline.

    A point is located at its building's centre (app/domain/catalog/points.py): for a large
    building the distance to that centre says nothing, being inside the outline does."""
    if latitude is None or longitude is None:
        return None
    if footprint is not None and outline_contains(footprint, longitude, latitude):
        return 0.0
    return haversine_meters(latitude, longitude, point.latitude, point.longitude)


def _candidate(
    stop: RouteStop,
    point: ServicePoint,
    footprint: dict[str, Any] | None,
    latitude: float | None,
    longitude: float | None,
) -> Candidate:
    distance = _distance_to_point(point, footprint, latitude, longitude)
    return Candidate(
        route_stop_id=stop.id,
        service_point_id=point.id,
        name=point.name,
        latitude=point.latitude,
        longitude=point.longitude,
        distance_m=distance,
    )


async def _load_pending_candidates(
    db: AsyncSession,
    *,
    route_id: uuid.UUID,
    floor_id: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
) -> tuple[list[Candidate], dict[uuid.UUID, RouteStop]]:
    rows = await db.execute(
        select(RouteStop, ServicePoint, Building.footprint)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .join(Floor, ServicePoint.floor_id == Floor.id)
        .join(Building, Floor.building_id == Building.id)
        .where(
            RouteStop.route_id == route_id,
            RouteStop.status == RouteStopStatus.PENDING,
            ServicePoint.floor_id == floor_id,
        )
        # IMPORTANT: lock the stops until the commit. The release (`release_stops`) updates only
        # rows still live, so it either waits for this check-in and spares the stop, or commits
        # first and this read no longer sees it — never a RELEASED stop flipped back.
        .with_for_update(of=RouteStop)
    )
    candidates: list[Candidate] = []
    stops: dict[uuid.UUID, RouteStop] = {}
    for stop, point, footprint in rows.all():
        candidates.append(_candidate(stop, point, footprint, latitude, longitude))
        stops[stop.id] = stop
    return candidates, stops


async def _load_open_checkins(
    db: AsyncSession,
    *,
    route_id: uuid.UUID,
    floor_id: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
) -> list[tuple[Candidate, Execution, RouteStop]]:
    rows = await db.execute(
        select(Execution, RouteStop, ServicePoint, Building.footprint)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .join(Floor, ServicePoint.floor_id == Floor.id)
        .join(Building, Floor.building_id == Building.id)
        .where(
            RouteStop.route_id == route_id,
            RouteStop.status == RouteStopStatus.IN_PROGRESS,
            Execution.checked_out_at.is_(None),
            ServicePoint.floor_id == floor_id,
        )
        # IMPORTANT: same lock as `_load_pending_candidates` — a check-out racing a release
        # either finishes the stop first or finds it released, never overwrites RELEASED.
        .with_for_update(of=RouteStop)
    )
    return [
        (_candidate(stop, point, footprint, latitude, longitude), execution, stop)
        for execution, stop, point, footprint in rows.all()
    ]


def _merge_flags(*groups: Sequence[str]) -> list[str]:
    merged: set[str] = set()
    for group in groups:
        merged.update(group)
    return sorted(merged)


def _build_scan(
    *,
    execution_id: uuid.UUID,
    qr_code_id: uuid.UUID,
    kind: QrScanKind,
    scanned_at: datetime,
    received_at: datetime,
    resolved: Candidate,
    geo_validation: GeoValidation,
    latitude: float | None,
    longitude: float | None,
) -> QrScan:
    return QrScan(
        execution_id=execution_id,
        qr_code_id=qr_code_id,
        kind=kind,
        scanned_at=scanned_at,
        received_at=received_at,  # server clock; `scanned_at` is the device's
        geo_validation=geo_validation,
        distance_m=resolved.distance_m,
        service_point_id=resolved.service_point_id,
        latitude=latitude,
        longitude=longitude,
    )
