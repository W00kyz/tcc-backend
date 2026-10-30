"""Completion by location (spec 2026-10-30-locais-externos §4): a place stop is finished by the
worker's position alone — one execution, no QR scan, refused (and left open) when the fix is
missing or beyond the check radius."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.catalog.models import ContractorCompany, FieldWorker, ServicePoint
from app.domain.execution.evidence_service import reconcile_evidence_flag
from app.domain.execution.geo import haversine_meters
from app.domain.execution.location_completion import (
    LocationRequired,
    NotALocationStop,
    OutOfRadius,
    StopAlreadyDone,
    complete_by_location,
)
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    QrScan,
)
from app.domain.execution.service import (
    RouteNotStarted,
    StopNotAssignedToWorker,
    StopReleased,
)
from app.domain.routing.models import ReleaseReason, Route, RouteStop, RouteStopStatus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import (
    ON_CAMPUS,
    make_place_point,
    make_points,
    make_route,
    make_service_type,
    make_worker,
)

_RADIUS_M = 50
_LAT, _LNG = ON_CAMPUS
# One metre of latitude, in degrees (the haversine uses a 6 371 km sphere).
_DEGREES_PER_METRE = 180 / (math.pi * 6_371_000)


@dataclass
class _Seed:
    worker: FieldWorker
    route: Route
    stop: RouteStop
    point: ServicePoint
    company: ContractorCompany


async def _seed(
    db: AsyncSession, *, requires_evidence: bool = False, building_stop: bool = False
) -> _Seed:
    """A started route with one stop: at an outdoor place, or in a building room when asked."""
    service_type = await make_service_type(db)
    company = service_type.contractor_company
    assert company is not None
    worker = await make_worker(db, company=company)
    if building_stop:
        point = (await make_points(db, service_type=service_type, names=["Sala 1"]))[0]
    else:
        point = await make_place_point(db, service_type=service_type, name="Jardim")
    route = await make_route(db, worker=worker, day=today_on_campus(), points=[point])
    route.stops[0].requires_evidence = requires_evidence
    await db.commit()
    return _Seed(worker=worker, route=route, stop=route.stops[0], point=point, company=company)


async def _complete(
    db: AsyncSession,
    seed: _Seed,
    *,
    latitude: float | None = _LAT,
    longitude: float | None = _LNG,
    idempotency_key: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    radius_m: int = _RADIUS_M,
    worker_id: uuid.UUID | None = None,
    completed_at: datetime | None = None,
    client_clock_offset_seconds: float | None = None,
    complaints_suggestions: str | None = None,
    route_stop_id: uuid.UUID | None = None,
) -> Execution:
    return await complete_by_location(
        db,
        route=seed.route,
        worker_id=worker_id or seed.worker.id,
        route_stop_id=route_stop_id or seed.stop.id,
        execution_id=execution_id or uuid.uuid4(),
        idempotency_key=idempotency_key or uuid.uuid4(),
        latitude=latitude,
        longitude=longitude,
        completed_at=completed_at or datetime.now(UTC),
        client_clock_offset_seconds=client_clock_offset_seconds,
        complaints_suggestions=complaints_suggestions,
        radius_m=radius_m,
        now=datetime.now(UTC),
    )


async def _count(db: AsyncSession, model: type) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _stop_status(db: AsyncSession, stop: RouteStop) -> RouteStopStatus:
    await db.refresh(stop)
    return stop.status


async def test_inside_the_radius_creates_one_done_execution_without_scan(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    completed_at = datetime.now(UTC) - timedelta(minutes=3)
    execution_id = uuid.uuid4()
    key = uuid.uuid4()

    execution = await _complete(
        db_session,
        seed,
        execution_id=execution_id,
        idempotency_key=key,
        completed_at=completed_at,
        complaints_suggestions="  Grama alta  ",
    )

    assert execution.id == execution_id
    assert execution.route_stop_id == seed.stop.id
    assert execution.field_worker_id == seed.worker.id
    assert execution.checked_in_at == execution.checked_out_at == completed_at
    assert execution.source is ExecutionSource.APP
    assert execution.idempotency_key == key
    assert execution.checkout_idempotency_key is None
    assert execution.complaints_suggestions == "Grama alta"
    assert execution.validation_flags == []
    assert execution.review_status is ExecutionReviewStatus.NONE
    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.DONE
    assert await _count(db_session, QrScan) == 0


async def test_blank_complaints_are_stored_as_null(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    execution = await _complete(db_session, seed, complaints_suggestions="   ")

    assert execution.complaints_suggestions is None


async def test_an_in_progress_stop_can_be_completed(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    seed.stop.status = RouteStopStatus.IN_PROGRESS
    await db_session.commit()

    await _complete(db_session, seed)

    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.DONE


async def test_exactly_at_the_radius_is_accepted(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    # A fix 30 m north, with the radius set to exactly that distance as the same haversine
    # measures it: `distance <= radius` accepts the boundary.
    latitude = _LAT + 30 * _DEGREES_PER_METRE
    distance = haversine_meters(latitude, _LNG, seed.point.latitude, seed.point.longitude)

    execution = await _complete(
        db_session, seed, latitude=latitude, longitude=_LNG, radius_m=math.ceil(distance)
    )

    assert execution.checked_out_at is not None


async def test_a_metre_beyond_the_radius_is_refused_and_the_stop_stays_open(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    latitude = _LAT + (_RADIUS_M + 1) * _DEGREES_PER_METRE

    with pytest.raises(OutOfRadius) as refused:
        await _complete(db_session, seed, latitude=latitude, longitude=_LNG)

    assert refused.value.radius_m == _RADIUS_M
    assert _RADIUS_M < refused.value.distance_m < _RADIUS_M + 2
    assert await _count(db_session, Execution) == 0
    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.PENDING


async def test_a_zero_radius_accepts_only_the_exact_point(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    await _complete(db_session, seed, radius_m=0)

    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.DONE


@pytest.mark.parametrize(("latitude", "longitude"), [(None, None), (_LAT, None), (None, _LNG)])
async def test_without_a_position_the_completion_is_refused(
    db_session: AsyncSession, latitude: float | None, longitude: float | None
) -> None:
    seed = await _seed(db_session)

    with pytest.raises(LocationRequired):
        await _complete(db_session, seed, latitude=latitude, longitude=longitude)

    assert await _count(db_session, Execution) == 0
    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.PENDING


async def test_replay_with_the_same_key_returns_the_first_execution(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    key = uuid.uuid4()

    first = await _complete(db_session, seed, idempotency_key=key)
    # A replay carries a new execution id and may even be refused on its own merits: the key wins.
    replay = await _complete(db_session, seed, idempotency_key=key, latitude=None, longitude=None)

    assert replay.id == first.id
    assert await _count(db_session, Execution) == 1


async def test_requires_evidence_flags_the_missing_photos_for_review(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session, requires_evidence=True)

    execution = await _complete(db_session, seed)

    assert execution.validation_flags == ["INSUFFICIENT_EVIDENCE"]
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW
    # Flagged, never refused: the stop is done and the photos follow.
    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.DONE


async def test_no_evidence_flag_when_the_type_does_not_require_it(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session, requires_evidence=False)

    execution = await _complete(db_session, seed)

    assert execution.validation_flags == []


async def _add_photo(db: AsyncSession, execution: Execution) -> None:
    db.add(
        EvidenceItem(
            execution_id=execution.id,
            kind=EvidenceKind.PHOTO,
            object_key=f"evidence/{uuid.uuid4()}.jpg",
            content_type="image/jpeg",
            byte_size=1,
            sha256="0" * 64,
            captured_at=datetime.now(UTC),
        )
    )
    await db.flush()
    await reconcile_evidence_flag(db, execution)


async def test_late_photos_clear_the_flag_only_at_two(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, requires_evidence=True)
    execution = await _complete(db_session, seed)

    await _add_photo(db_session, execution)
    assert execution.validation_flags == ["INSUFFICIENT_EVIDENCE"]
    assert execution.review_status is ExecutionReviewStatus.PENDING_REVIEW

    await _add_photo(db_session, execution)
    assert execution.validation_flags == []
    assert execution.review_status.value == "NONE"  # `is` would be narrowed by mypy


async def test_a_skewed_device_clock_is_flagged_past_300_seconds(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)

    skewed = await _complete(db_session, seed, client_clock_offset_seconds=301.0)
    steady = await _complete(
        db_session, await _seed_second(db_session), client_clock_offset_seconds=300.0
    )

    assert skewed.validation_flags == ["CLOCK_SKEW"]
    assert skewed.clock_skew_seconds == 301.0
    assert skewed.review_status is ExecutionReviewStatus.PENDING_REVIEW
    assert steady.validation_flags == []
    assert steady.clock_skew_seconds is None


async def _seed_second(db: AsyncSession) -> _Seed:
    """Another independent worker, place and route (unique names and CNPJ per seed)."""
    service_type = await make_service_type(db, company_name="Verde", type_name="Jardinagem")
    company = service_type.contractor_company
    assert company is not None
    worker = await make_worker(db, company=company)
    point = await make_place_point(
        db, service_type=service_type, name="Canteiro", place_name="Praça do bosque"
    )
    route = await make_route(db, worker=worker, day=today_on_campus(), points=[point])
    await db.commit()
    return _Seed(worker=worker, route=route, stop=route.stops[0], point=point, company=company)


async def test_a_stop_in_a_building_is_not_a_location_stop(db_session: AsyncSession) -> None:
    seed = await _seed(db_session, building_stop=True)

    with pytest.raises(NotALocationStop):
        await _complete(db_session, seed)

    assert await _count(db_session, Execution) == 0
    assert await _stop_status(db_session, seed.stop) is RouteStopStatus.PENDING


async def test_a_released_stop_answers_stop_released(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    seed.stop.status = RouteStopStatus.RELEASED
    seed.stop.release_reason = ReleaseReason.IMPEDIMENT
    seed.stop.released_at = datetime.now(UTC)
    await db_session.commit()

    with pytest.raises(StopReleased) as refused:
        await _complete(db_session, seed)

    assert refused.value.route_stop_id == seed.stop.id
    assert refused.value.release_reason is ReleaseReason.IMPEDIMENT
    assert await _count(db_session, Execution) == 0


async def test_a_stop_done_by_another_execution_is_a_conflict(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _complete(db_session, seed)

    with pytest.raises(StopAlreadyDone, match="already"):
        await _complete(db_session, seed)

    assert await _count(db_session, Execution) == 1


async def test_another_workers_route_is_refused(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    other = await make_worker(db_session, company=seed.company)
    await db_session.commit()

    with pytest.raises(StopNotAssignedToWorker):
        await _complete(db_session, seed, worker_id=other.id)

    assert await _count(db_session, Execution) == 0


async def test_a_stop_of_a_different_route_is_refused(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    other = await _seed_second(db_session)

    with pytest.raises(StopNotAssignedToWorker):
        await _complete(db_session, seed, route_stop_id=other.stop.id)


async def test_an_unstarted_route_is_refused(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    seed.route.started_at = None
    await db_session.commit()

    with pytest.raises(RouteNotStarted):
        await _complete(db_session, seed)
