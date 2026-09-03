import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.execution.models import (
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    GeoValidation,
    ManualCompletion,
    QrScan,
    QrScanKind,
)
from app.domain.execution.service import (
    AmbiguousRoom,
    FloorNotOnRoute,
    NoOpenCheckIn,
    QrRevoked,
    QrSignatureInvalid,
    RouteCancelled,
    RouteNotStarted,
    StopAlreadyDone,
    StopAlreadyManuallyCompleted,
    StopNotAssignedToWorker,
    StopNotOnRoute,
    check_in,
    check_out,
    complete_manually,
    start_route,
)
from app.domain.execution.validation import (
    FLAG_CLOCK_SKEW,
    FLAG_GPS_UNAVAILABLE,
    FLAG_OUT_OF_RADIUS,
    FLAG_OUTSIDE_SCHEDULE,
    FLAG_QR_SUPERSEDED,
)
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex, sign_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.routing.models import (
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
    StopAssignment,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_PRIVATE_KEY_HEX = "11" * 32
_PUBLIC_KEY_HEX = derive_public_key_hex(_PRIVATE_KEY_HEX)
_RADIUS_M = 50

# Reference point and a neighbour ~15 m away (same floor, distinct room).
_LAT = -7.2
_LON = -35.9
_NEAR_LAT = -7.20013
_NEAR_LON = -35.90013
# ~220 m north of the reference point — comfortably outside a 50 m radius.
_FAR_LAT = -7.202
_FAR_LON = -35.9


@dataclass
class _Seed:
    manager: User
    worker: FieldWorker
    route: Route
    floor: Floor
    qr_code: QrCode
    stops: list[RouteStop]
    points: list[ServicePoint]

    @property
    def qr_payload(self) -> str:
        return self.qr_code.public_code


async def _seed(
    db: AsyncSession,
    *,
    rooms: list[tuple[str, float, float]] | None = None,
    started: bool = True,
    route_status: RouteStatus | None = None,
    qr_version: int = 1,
    qr_status: QrCodeStatus = QrCodeStatus.ACTIVE,
    tag: str = "1",
) -> _Seed:
    # `tag` keeps the unique columns (manager email, company CNPJ) distinct when a test
    # seeds two independent routes in one session.
    rooms = rooms or [("Sala 101", _LAT, _LON)]
    manager = User(
        name="Larissa",
        email=f"larissa-{tag}@pu.ufcg.edu.br",
        password_hash="x",
        role=UserRole.MANAGER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj=(tag.rjust(2, "0") + "345678000199")[:14])
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([manager, company, building])
    await db.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db.add(floor)
    await db.flush()

    points = [
        ServicePoint(floor_id=floor.id, name=name, description="Sala", latitude=lat, longitude=lon)
        for name, lat, lon in rooms
    ]
    worker = FieldWorker(full_name="João", contractor_company_id=company.id)
    db.add_all([*points, worker])
    await db.flush()

    payload = sign_qr_payload(
        floor_id=floor.id, version=qr_version, private_key_hex=_PRIVATE_KEY_HEX
    )
    qr_code = QrCode(
        floor_id=floor.id, public_code=payload, secret=b"sig", version=qr_version, status=qr_status
    )
    status = route_status or (RouteStatus.IN_PROGRESS if started else RouteStatus.PLANNED)
    route = Route(
        field_worker_id=worker.id,
        route_date=datetime.now(UTC).date(),
        status=status,
        started_at=datetime.now(UTC) if started else None,
    )
    db.add_all([qr_code, route])
    await db.flush()

    stops = [
        RouteStop(route_id=route.id, service_point_id=point.id, order_index=index + 1)
        for index, point in enumerate(points)
    ]
    db.add_all(stops)
    await db.commit()
    return _Seed(
        manager=manager,
        worker=worker,
        route=route,
        floor=floor,
        qr_code=qr_code,
        stops=stops,
        points=points,
    )


async def _do_check_in(
    db: AsyncSession,
    seed: _Seed,
    *,
    latitude: float | None = _LAT,
    longitude: float | None = _LON,
    scanned_at: datetime | None = None,
    idempotency_key: uuid.UUID | None = None,
    chosen_route_stop_id: uuid.UUID | None = None,
    qr_payload: str | None = None,
    execution_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
) -> Execution:
    return await check_in(
        db,
        route=seed.route,
        worker_id=seed.worker.id,
        qr_payload=qr_payload or seed.qr_payload,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=latitude,
        longitude=longitude,
        scanned_at=scanned_at or datetime.now(UTC),
        idempotency_key=idempotency_key or uuid.uuid4(),
        chosen_route_stop_id=chosen_route_stop_id,
        radius_m=_RADIUS_M,
        execution_id=execution_id,
        client_clock_offset_seconds=client_clock_offset_seconds,
    )


async def _do_check_out(
    db: AsyncSession,
    seed: _Seed,
    *,
    latitude: float | None = _LAT,
    longitude: float | None = _LON,
    scanned_at: datetime | None = None,
    checkout_idempotency_key: uuid.UUID | None = None,
    chosen_route_stop_id: uuid.UUID | None = None,
    client_clock_offset_seconds: float | None = None,
) -> Execution:
    return await check_out(
        db,
        route=seed.route,
        worker_id=seed.worker.id,
        qr_payload=seed.qr_payload,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=latitude,
        longitude=longitude,
        scanned_at=scanned_at or datetime.now(UTC),
        checkout_idempotency_key=checkout_idempotency_key or uuid.uuid4(),
        chosen_route_stop_id=chosen_route_stop_id,
        radius_m=_RADIUS_M,
        client_clock_offset_seconds=client_clock_offset_seconds,
    )


async def _manual(
    db: AsyncSession, seed: _Seed, stop: RouteStop, *, reason: str = "Verified in person."
) -> Execution:
    return await complete_manually(
        db,
        route=seed.route,
        stop=stop,
        actor_id=seed.manager.id,
        reason=reason,
        completed_at=datetime.now(UTC),
    )


async def _add_assignment(db: AsyncSession, seed: _Seed, stop: RouteStop) -> None:
    db.add(
        StopAssignment(
            route_stop_id=stop.id,
            field_worker_id=seed.worker.id,
            sequence=1,
            assigned_by=seed.manager.id,
        )
    )
    await db.commit()


async def _scans(db: AsyncSession, execution_id: uuid.UUID) -> list[QrScan]:
    result = await db.scalars(select(QrScan).where(QrScan.execution_id == execution_id))
    return list(result)


async def _executions_for_stop(db: AsyncSession, stop_id: uuid.UUID) -> list[Execution]:
    result = await db.scalars(select(Execution).where(Execution.route_stop_id == stop_id))
    return list(result)


# --- start_route (unchanged behaviour, kept green) --------------------------------------------


async def test_start_route_sets_status(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, started=False)

    await start_route(
        db_session, route=seed.route, latitude=_LAT, longitude=_LON, started_at=datetime.now(UTC)
    )

    await db_session.refresh(seed.route)
    assert seed.route.status is RouteStatus.IN_PROGRESS


# --- check_in --------------------------------------------------------------------------------


async def test_check_in_resolves_single_stop_on_floor_and_marks_in_progress(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)

    execution = await _do_check_in(db_session, seed)

    assert execution.route_stop_id == seed.stops[0].id
    assert execution.source is ExecutionSource.APP
    assert execution.validation_flags == []
    assert execution.review_status is ExecutionReviewStatus.NONE
    scans = await _scans(db_session, execution.id)
    assert len(scans) == 1
    assert scans[0].kind is QrScanKind.CHECK_IN
    assert scans[0].geo_validation is GeoValidation.VALIDATED
    assert scans[0].service_point_id == seed.points[0].id
    assert scans[0].distance_m is not None
    await db_session.refresh(seed.stops[0])
    assert seed.stops[0].status is RouteStopStatus.IN_PROGRESS


async def test_check_in_out_of_radius_flags_and_pending_review(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    execution = await _do_check_in(db_session, seed, latitude=_FAR_LAT, longitude=_FAR_LON)

    assert execution.validation_flags == [FLAG_OUT_OF_RADIUS]
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW
    scans = await _scans(db_session, execution.id)
    assert scans[0].geo_validation is GeoValidation.OUT_OF_RADIUS


async def test_check_in_no_gps_marks_not_validated(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    execution = await _do_check_in(db_session, seed, latitude=None, longitude=None)

    assert FLAG_GPS_UNAVAILABLE in execution.validation_flags
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW
    scans = await _scans(db_session, execution.id)
    assert scans[0].geo_validation is GeoValidation.NOT_VALIDATED
    assert scans[0].latitude is None
    assert scans[0].longitude is None
    assert scans[0].distance_m is None


async def test_check_in_two_stops_same_floor_raises_ambiguous(db_session: AsyncSession) -> None:
    seed = await _seed(
        db_session,
        rooms=[("Sala 101", _LAT, _LON), ("Sala 102", _NEAR_LAT, _NEAR_LON)],
    )

    with pytest.raises(AmbiguousRoom) as excinfo:
        await _do_check_in(db_session, seed)

    candidates = excinfo.value.candidates
    assert [c.route_stop_id for c in candidates] == [seed.stops[0].id, seed.stops[1].id]


async def test_check_in_ambiguous_then_resubmit_with_choice_succeeds(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(
        db_session,
        rooms=[("Sala 101", _LAT, _LON), ("Sala 102", _NEAR_LAT, _NEAR_LON)],
    )

    with pytest.raises(AmbiguousRoom):
        await _do_check_in(db_session, seed)

    execution = await _do_check_in(db_session, seed, chosen_route_stop_id=seed.stops[1].id)

    assert execution.route_stop_id == seed.stops[1].id
    await db_session.refresh(seed.stops[1])
    assert seed.stops[1].status is RouteStopStatus.IN_PROGRESS
    await db_session.refresh(seed.stops[0])
    assert seed.stops[0].status is RouteStopStatus.PENDING


async def test_check_in_floor_not_on_route_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    other_floor = Floor(building_id=seed.floor.building_id, label="1º andar")
    db_session.add(other_floor)
    await db_session.flush()
    other_payload = sign_qr_payload(
        floor_id=other_floor.id, version=1, private_key_hex=_PRIVATE_KEY_HEX
    )
    db_session.add(
        QrCode(floor_id=other_floor.id, public_code=other_payload, secret=b"sig", version=1)
    )
    await db_session.commit()

    with pytest.raises(FloorNotOnRoute):
        await _do_check_in(db_session, seed, qr_payload=other_payload)


async def test_check_in_revoked_qr_raises_qr_revoked(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, qr_status=QrCodeStatus.REVOKED)

    with pytest.raises(QrRevoked):
        await _do_check_in(db_session, seed)


async def test_check_in_superseded_qr_flags_but_succeeds(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, qr_version=1)
    newer_payload = sign_qr_payload(
        floor_id=seed.floor.id, version=2, private_key_hex=_PRIVATE_KEY_HEX
    )
    db_session.add(
        QrCode(floor_id=seed.floor.id, public_code=newer_payload, secret=b"sig", version=2)
    )
    await db_session.commit()

    execution = await _do_check_in(db_session, seed)

    assert FLAG_QR_SUPERSEDED in execution.validation_flags
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW


async def test_check_in_outside_schedule_window_flags(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    today = datetime.now(UTC).date()
    seed.stops[0].expected_arrival_from = datetime.combine(
        today, datetime.min.time(), tzinfo=UTC
    ) + timedelta(hours=8)
    seed.stops[0].expected_arrival_to = seed.stops[0].expected_arrival_from + timedelta(minutes=30)
    await db_session.commit()
    noon = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    execution = await _do_check_in(db_session, seed, scanned_at=noon)

    assert FLAG_OUTSIDE_SCHEDULE in execution.validation_flags


async def test_check_in_idempotent(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    key = uuid.uuid4()

    first = await _do_check_in(db_session, seed, idempotency_key=key)
    second = await _do_check_in(db_session, seed, idempotency_key=key)

    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count()).select_from(QrScan).where(QrScan.execution_id == first.id)
    )
    assert count == 1


async def test_check_in_route_cancelled_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, route_status=RouteStatus.CANCELLED)

    with pytest.raises(RouteCancelled):
        await _do_check_in(db_session, seed)


async def test_check_in_before_start_is_rejected(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, started=False)

    with pytest.raises(RouteNotStarted):
        await _do_check_in(db_session, seed)


async def test_check_in_by_a_different_worker_is_rejected(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    with pytest.raises(StopNotAssignedToWorker):
        await check_in(
            db_session,
            route=seed.route,
            worker_id=uuid.uuid4(),
            qr_payload=seed.qr_payload,
            public_key_hex=_PUBLIC_KEY_HEX,
            latitude=_LAT,
            longitude=_LON,
            scanned_at=datetime.now(UTC),
            idempotency_key=uuid.uuid4(),
            chosen_route_stop_id=None,
            radius_m=_RADIUS_M,
        )


async def test_check_in_with_a_forged_qr_is_rejected(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    forged = sign_qr_payload(floor_id=uuid.uuid4(), version=1, private_key_hex="99" * 32)

    with pytest.raises(QrSignatureInvalid):
        await _do_check_in(db_session, seed, qr_payload=forged)


async def test_check_in_uses_app_supplied_execution_id(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    fixed_id = uuid.uuid4()

    execution = await _do_check_in(db_session, seed, execution_id=fixed_id)

    assert execution.id == fixed_id


async def test_check_in_large_clock_offset_flags_and_pending_review(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)

    execution = await _do_check_in(db_session, seed, client_clock_offset_seconds=600.0)

    assert execution.clock_skew_seconds == 600.0
    assert FLAG_CLOCK_SKEW in execution.validation_flags
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW


async def test_check_in_small_clock_offset_is_not_flagged(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    execution = await _do_check_in(db_session, seed, client_clock_offset_seconds=60.0)

    assert execution.clock_skew_seconds is None
    assert FLAG_CLOCK_SKEW not in execution.validation_flags


async def test_check_out_large_clock_offset_flags_execution(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _do_check_in(db_session, seed)

    execution = await _do_check_out(db_session, seed, client_clock_offset_seconds=-600.0)

    assert FLAG_CLOCK_SKEW in execution.validation_flags
    assert execution.clock_skew_seconds == -600.0
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW


# --- check_out -------------------------------------------------------------------------------


async def test_check_out_closes_execution_and_marks_done(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    db_session.add(
        StopAssignment(
            route_stop_id=seed.stops[0].id,
            field_worker_id=seed.worker.id,
            sequence=1,
            assigned_by=seed.manager.id,
        )
    )
    await db_session.commit()
    await _do_check_in(db_session, seed)

    execution = await _do_check_out(db_session, seed)

    assert execution.checked_out_at is not None
    await db_session.refresh(seed.stops[0])
    assert seed.stops[0].status is RouteStopStatus.DONE
    scans = await _scans(db_session, execution.id)
    assert {scan.kind for scan in scans} == {QrScanKind.CHECK_IN, QrScanKind.CHECK_OUT}
    assignment = await db_session.scalar(
        select(StopAssignment).where(StopAssignment.route_stop_id == seed.stops[0].id)
    )
    assert assignment is not None
    assert assignment.outcome is not None
    assert assignment.outcome.value == "EXECUTED"


async def test_check_out_without_open_checkin_raises_no_open_checkin(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)

    with pytest.raises(NoOpenCheckIn):
        await _do_check_out(db_session, seed)


async def test_check_out_idempotent_by_checkout_key(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _do_check_in(db_session, seed)
    key = uuid.uuid4()

    first = await _do_check_out(db_session, seed, checkout_idempotency_key=key)
    second = await _do_check_out(db_session, seed, checkout_idempotency_key=key)

    assert first.id == second.id
    checkout_scans = [
        scan for scan in await _scans(db_session, first.id) if scan.kind is QrScanKind.CHECK_OUT
    ]
    assert len(checkout_scans) == 1


async def test_check_out_on_cancelled_route_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _do_check_in(db_session, seed)
    seed.route.status = RouteStatus.CANCELLED
    await db_session.commit()

    with pytest.raises(RouteCancelled):
        await _do_check_out(db_session, seed)


async def test_check_out_reopens_resolved_review_on_new_flag(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    checkin = await _do_check_in(db_session, seed)
    assert checkin.validation_flags == []
    # Simulate a manager clearing the check-in review before the worker checks out.
    checkin.review_status = ExecutionReviewStatus.RESOLVED
    await db_session.commit()

    execution = await _do_check_out(db_session, seed, latitude=_FAR_LAT, longitude=_FAR_LON)

    assert FLAG_OUT_OF_RADIUS in execution.validation_flags
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW


async def test_check_out_two_open_checkins_same_floor_raises_ambiguous(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(
        db_session,
        rooms=[("Sala 101", _LAT, _LON), ("Sala 102", _NEAR_LAT, _NEAR_LON)],
    )
    await _do_check_in(db_session, seed, chosen_route_stop_id=seed.stops[0].id)
    await _do_check_in(db_session, seed, chosen_route_stop_id=seed.stops[1].id)

    with pytest.raises(AmbiguousRoom) as excinfo:
        await _do_check_out(db_session, seed)

    assert [c.route_stop_id for c in excinfo.value.candidates] == [
        seed.stops[0].id,
        seed.stops[1].id,
    ]


# --- complete_manually ----------------------------------------------------------------------


async def test_complete_manually_creates_synthetic_execution(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    db_session.add(
        StopAssignment(
            route_stop_id=seed.stops[0].id,
            field_worker_id=seed.worker.id,
            sequence=1,
            assigned_by=seed.manager.id,
        )
    )
    await db_session.commit()
    completed_at = datetime.now(UTC)

    execution = await complete_manually(
        db_session,
        route=seed.route,
        stop=seed.stops[0],
        actor_id=seed.manager.id,
        reason="Worker phone died; verified in person.",
        completed_at=completed_at,
    )

    assert execution.source is ExecutionSource.MANAGER_MANUAL
    assert await _scans(db_session, execution.id) == []
    manual = await db_session.scalar(
        select(ManualCompletion).where(ManualCompletion.execution_id == execution.id)
    )
    assert manual is not None
    assert manual.reason == "Worker phone died; verified in person."
    assert manual.completed_by == seed.manager.id
    await db_session.refresh(seed.stops[0])
    assert seed.stops[0].status is RouteStopStatus.DONE
    assignment = await db_session.scalar(
        select(StopAssignment).where(StopAssignment.route_stop_id == seed.stops[0].id)
    )
    assert assignment is not None
    assert assignment.outcome is not None
    assert assignment.outcome.value == "EXECUTED"


async def test_complete_manually_twice_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await complete_manually(
        db_session,
        route=seed.route,
        stop=seed.stops[0],
        actor_id=seed.manager.id,
        reason="First.",
        completed_at=datetime.now(UTC),
    )

    with pytest.raises(StopAlreadyManuallyCompleted):
        await complete_manually(
            db_session,
            route=seed.route,
            stop=seed.stops[0],
            actor_id=seed.manager.id,
            reason="Second.",
            completed_at=datetime.now(UTC),
        )


async def test_complete_manually_on_done_stop_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _do_check_in(db_session, seed)
    await _do_check_out(db_session, seed)

    with pytest.raises(StopAlreadyDone):
        await _manual(db_session, seed, seed.stops[0])


async def test_complete_manually_on_in_progress_stop_closes_the_open_checkin(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    await _add_assignment(db_session, seed, seed.stops[0])
    checkin = await _do_check_in(db_session, seed)
    assert checkin.checked_out_at is None

    manual = await _manual(db_session, seed, seed.stops[0])

    assert manual.id == checkin.id
    assert manual.source is ExecutionSource.APP
    assert manual.checked_out_at is not None
    assert len(await _executions_for_stop(db_session, seed.stops[0].id)) == 1
    row = await db_session.scalar(
        select(ManualCompletion).where(ManualCompletion.route_stop_id == seed.stops[0].id)
    )
    assert row is not None
    assert row.execution_id == checkin.id
    await db_session.refresh(seed.stops[0])
    assert seed.stops[0].status is RouteStopStatus.DONE
    assignment = await db_session.scalar(
        select(StopAssignment).where(StopAssignment.route_stop_id == seed.stops[0].id)
    )
    assert assignment is not None
    assert assignment.outcome is not None
    assert assignment.outcome.value == "EXECUTED"


async def test_complete_manually_on_cancelled_route_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    seed.route.status = RouteStatus.CANCELLED
    await db_session.commit()

    with pytest.raises(RouteCancelled):
        await _manual(db_session, seed, seed.stops[0])


async def test_complete_manually_stop_from_another_route_raises(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, tag="1")
    other = await _seed(db_session, tag="2")

    with pytest.raises(StopNotOnRoute):
        await _manual(db_session, seed, other.stops[0])
