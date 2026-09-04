"""Route start (RF34), check-in (RF29), check-out (RF31), manager manual completion (RF33).
Check-in is floor-QR-first: the QR identifies a floor, `resolve_room` picks the room from
the worker's PENDING stops there, and the five anti-fraud layers (signature, QR status, GPS
radius, room match, schedule window) attach flags without blocking a reviewable check-in
(spec §4.2, §4.4).

DO NOT CHANGE: this module is the documented exception to "service layer flushes, never
commits" — check-in/check-out/manual-completion each own and commit a transaction, as
`start_route` already does."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import ServicePoint
from app.domain.execution.answers import AnswerIn, build_answers
from app.domain.execution.geo import haversine_meters
from app.domain.execution.models import (
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    GeoValidation,
    ManualCompletion,
    QrScan,
    QrScanKind,
)
from app.domain.execution.validation import (
    FLAG_QR_SUPERSEDED,
    Candidate,
    ScheduleWindow,
    clock_skew_flag,
    resolve_room,
    schedule_flag,
)
from app.domain.forms.models import FormVersion, FormVersionStatus
from app.domain.qr.crypto import QrPayload, decode_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
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


class RouteCancelled(Exception):
    """The route was cancelled — it can no longer take check-ins or completions."""


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


class StopAlreadyDone(Exception):
    pass


class StopAlreadyManuallyCompleted(Exception):
    """A `manual_completions` row already exists for this stop (its unique constraint)."""


class NoOpenCheckIn(Exception):
    """Check-out found no IN_PROGRESS stop with an open execution on the scanned floor."""


class StopNotOnRoute(Exception):
    """The `stop` passed to `complete_manually` does not belong to the given `route`."""


class FormVersionMismatch(Exception):
    """The check-out's `form_version_id`/`answers` do not fit the stop (Ruling 7): missing one
    of the pair, a non-PUBLISHED version, the wrong service type's form, or a stop with no
    service type at all."""


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
    execution_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
) -> Execution:
    """Floor-QR check-in: resolves the room, attaches anti-fraud flags, moves the stop to
    IN_PROGRESS. Pass `chosen_route_stop_id` on the re-submit after an `AmbiguousRoom`;
    `latitude`/`longitude` are `None` when the device has no GPS fix. The offline app supplies
    `execution_id` so a retried check-in re-uses the same row, and reports its measured
    `client_clock_offset_seconds` (server minus device clock) so implausible skew is flagged."""
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
    flags = _merge_flags(resolution.flags, _schedule_flags(stop, scanned_at), qr_flags, skew_flags)

    now = datetime.now(UTC)
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
    execution_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
    form_version_id: uuid.UUID | None = None,
    answers: list[AnswerIn] | None = None,
) -> Execution:
    """Close the open check-in on the scanned floor: sets `checked_out_at`, merges any new
    flags into the execution, marks the stop DONE and its assignment EXECUTED. `execution_id`
    is accepted for API symmetry with check-in but ignored here — check-out resolves the open
    execution by floor, it does not create one. `client_clock_offset_seconds` still flags
    implausible skew."""
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
    checkout_flags = _merge_flags(
        resolution.flags, _schedule_flags(stop, scanned_at), qr_flags, skew_flags
    )
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
            resolved=resolved,
            geo_validation=resolution.geo_validation,
            latitude=latitude,
            longitude=longitude,
        )
    )
    stop.status = RouteStopStatus.DONE
    await _mark_assignment_executed(db, stop.id)
    await _persist_answers(
        db, execution=execution, stop=stop, form_version_id=form_version_id, answers=answers
    )
    await db.commit()
    return execution


async def complete_manually(
    db: AsyncSession,
    *,
    route: Route,
    stop: RouteStop,
    actor_id: uuid.UUID,
    reason: str,
    completed_at: datetime,
) -> Execution:
    """Manager closes a stop the worker could not (RF33) with an audited `manual_completions`
    row. An IN_PROGRESS stop's open check-in `Execution` is closed in place (no orphan row);
    a PENDING stop gets a synthetic MANAGER_MANUAL execution. No QR scan either way."""
    if stop.route_id != route.id:
        raise StopNotOnRoute(
            f'Route stop "{stop.id}" belongs to route "{stop.route_id}", not "{route.id}".'
        )
    already_manual = await db.scalar(
        select(ManualCompletion).where(ManualCompletion.route_stop_id == stop.id)
    )
    if already_manual is not None:
        raise StopAlreadyManuallyCompleted(
            f'Route stop "{stop.id}" was already completed manually.'
        )
    if stop.status is RouteStopStatus.DONE:
        raise StopAlreadyDone(f'Route stop "{stop.id}" is already DONE.')
    if route.status is RouteStatus.CANCELLED:
        raise RouteCancelled(f'Route "{route.id}" was cancelled and cannot be completed.')

    now = datetime.now(UTC)
    open_checkin = await db.scalar(
        select(Execution).where(
            Execution.route_stop_id == stop.id, Execution.checked_out_at.is_(None)
        )
    )
    if open_checkin is not None:
        open_checkin.checked_out_at = completed_at
        execution = open_checkin
    else:
        execution = Execution(
            route_stop_id=stop.id,
            field_worker_id=route.field_worker_id,
            checked_in_at=completed_at,
            checked_out_at=completed_at,
            synced_at=now,
            source=ExecutionSource.MANAGER_MANUAL,
            idempotency_key=uuid.uuid4(),
            review_status=ExecutionReviewStatus.NONE,
            validation_flags=[],
        )
        db.add(execution)
        await db.flush()

    db.add(
        ManualCompletion(
            route_stop_id=stop.id,
            execution_id=execution.id,
            completed_by=actor_id,
            reason=reason,
            completed_at=completed_at,
        )
    )
    stop.status = RouteStopStatus.DONE
    await _mark_assignment_executed(db, stop.id)
    await db.commit()
    return execution


# --- internal helpers -----------------------------------------------------------------------


async def _persist_answers(
    db: AsyncSession,
    *,
    execution: Execution,
    stop: RouteStop,
    form_version_id: uuid.UUID | None,
    answers: list[AnswerIn] | None,
) -> None:
    """RF37/RF38 (Ruling 6): persist the check-out form answers exactly as sent, inside the
    check-out transaction. `form_version_id` and `answers` come as a pair — one without the
    other, or a stop with no service type, is a `FormVersionMismatch` (Ruling 7)."""
    if form_version_id is None and answers is None:
        return
    if form_version_id is None or answers is None:
        raise FormVersionMismatch(
            "check-out needs both form_version_id and answers, or neither; "
            f"got form_version_id={form_version_id!r}, answers={'set' if answers else None}."
        )
    if stop.service_type_id is None:
        raise FormVersionMismatch(
            f'Route stop "{stop.id}" has no service_type_id; it cannot carry form answers.'
        )
    version = await db.scalar(
        select(FormVersion)
        .where(FormVersion.id == form_version_id)
        .options(selectinload(FormVersion.questions), selectinload(FormVersion.form))
    )
    if (
        version is None
        or version.status != FormVersionStatus.PUBLISHED
        or version.form.service_type_id != stop.service_type_id
    ):
        raise FormVersionMismatch(
            f'Form version "{form_version_id}" is not a PUBLISHED version of the form for '
            f'service type "{stop.service_type_id}".'
        )
    execution.form_version_id = form_version_id
    db.add_all(build_answers(execution_id=execution.id, form_version=version, answers=answers))


def _guard_route(route: Route, worker_id: uuid.UUID, *, require_started: bool) -> None:
    if route.status is RouteStatus.CANCELLED:
        raise RouteCancelled(f'Route "{route.id}" was cancelled and cannot take check-ins.')
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


def _candidate(
    stop: RouteStop, point: ServicePoint, latitude: float | None, longitude: float | None
) -> Candidate:
    distance = (
        haversine_meters(latitude, longitude, point.latitude, point.longitude)
        if latitude is not None and longitude is not None
        else None
    )
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
        select(RouteStop, ServicePoint)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .where(
            RouteStop.route_id == route_id,
            RouteStop.status == RouteStopStatus.PENDING,
            ServicePoint.floor_id == floor_id,
        )
    )
    candidates: list[Candidate] = []
    stops: dict[uuid.UUID, RouteStop] = {}
    for stop, point in rows.all():
        candidates.append(_candidate(stop, point, latitude, longitude))
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
        select(Execution, RouteStop, ServicePoint)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .where(
            RouteStop.route_id == route_id,
            RouteStop.status == RouteStopStatus.IN_PROGRESS,
            Execution.checked_out_at.is_(None),
            ServicePoint.floor_id == floor_id,
        )
    )
    return [
        (_candidate(stop, point, latitude, longitude), execution, stop)
        for execution, stop, point in rows.all()
    ]


def _schedule_flags(stop: RouteStop, scanned_at: datetime) -> list[str]:
    window = ScheduleWindow(
        arrival_from=stop.expected_arrival_from, arrival_to=stop.expected_arrival_to
    )
    return schedule_flag(window, scanned_at)


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
        received_at=datetime.now(UTC),  # server clock; `scanned_at` is the device's
        geo_validation=geo_validation,
        distance_m=resolved.distance_m,
        service_point_id=resolved.service_point_id,
        latitude=latitude,
        longitude=longitude,
    )


async def _mark_assignment_executed(db: AsyncSession, route_stop_id: uuid.UUID) -> None:
    assignment = await db.scalar(
        select(StopAssignment)
        .where(StopAssignment.route_stop_id == route_stop_id)
        .order_by(StopAssignment.sequence.desc())
        .limit(1)
    )
    if assignment is not None:
        assignment.outcome = StopAssignmentOutcome.EXECUTED
