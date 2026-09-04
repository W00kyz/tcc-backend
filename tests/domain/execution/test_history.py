"""RF43 — the manager execution-history read model: a filterable, paginated list, a detail
row that carries the scans/evidence/manual-completion, and review resolution."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.execution.history import (
    ExecutionFilters,
    ExecutionNotFound,
    ReviewNotPending,
    get_execution_detail,
    list_executions,
    resolve_review,
)
from app.domain.execution.models import (
    Answer,
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    GeoValidation,
    QrScan,
    QrScanKind,
)
from app.domain.forms.models import (
    Form,
    FormQuestion,
    FormVersion,
    FormVersionStatus,
    QuestionType,
)
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus
from sqlalchemy.ext.asyncio import AsyncSession

# Anchored to a fixed noon so every relative offset below (-1h, -25h) lands on its
# intended UTC calendar day no matter the wall-clock time the suite runs at — a bare
# `datetime.now(UTC)` makes `test_filter_by_date_range` flaky just after UTC midnight.
_NOW = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


@dataclass
class _World:
    worker_a: uuid.UUID
    worker_b: uuid.UUID
    building_a: uuid.UUID
    building_b: uuid.UUID
    point_a: uuid.UUID
    point_b: uuid.UUID
    exec_today_a: uuid.UUID  # worker_a, building_a, PENDING_REVIEW, OUT_OF_RADIUS scan
    exec_old_b: uuid.UUID  # worker_a, building_b, yesterday, VALIDATED scan
    exec_manual_a: uuid.UUID  # worker_b, building_a, MANAGER_MANUAL, no scan


async def _floor_with_point(
    db: AsyncSession, building_id: uuid.UUID, label: str, point_name: str
) -> tuple[uuid.UUID, uuid.UUID]:
    floor = Floor(building_id=building_id, label=label)
    db.add(floor)
    await db.flush()
    point = ServicePoint(
        floor_id=floor.id, name=point_name, description="Sala", latitude=-7.2, longitude=-35.9
    )
    db.add(point)
    await db.flush()
    return floor.id, point.id


async def _execution(
    db: AsyncSession,
    *,
    worker_id: uuid.UUID,
    route_id: uuid.UUID,
    point_id: uuid.UUID,
    checked_in_at: datetime,
    source: ExecutionSource,
    review_status: ExecutionReviewStatus,
    stop_status: RouteStopStatus = RouteStopStatus.DONE,
) -> tuple[uuid.UUID, uuid.UUID]:
    stop = RouteStop(
        route_id=route_id, service_point_id=point_id, order_index=1, status=stop_status
    )
    db.add(stop)
    await db.flush()
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker_id,
        checked_in_at=checked_in_at,
        checked_out_at=checked_in_at + timedelta(minutes=20),
        synced_at=checked_in_at,
        source=source,
        idempotency_key=uuid.uuid4(),
        review_status=review_status,
    )
    db.add(execution)
    await db.flush()
    return execution.id, stop.id


async def _checkin_scan(
    db: AsyncSession,
    *,
    execution_id: uuid.UUID,
    qr_code_id: uuid.UUID,
    geo: GeoValidation,
) -> None:
    db.add(
        QrScan(
            execution_id=execution_id,
            qr_code_id=qr_code_id,
            scanned_at=_NOW,
            received_at=_NOW,
            geo_validation=geo,
            kind=QrScanKind.CHECK_IN,
        )
    )
    await db.flush()


async def _seed_world(db: AsyncSession) -> _World:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building_a = Building(name="Bloco A", campus_area="CCT")
    building_b = Building(name="Bloco B", campus_area="CCT")
    db.add_all([company, building_a, building_b])
    await db.flush()

    floor_a, point_a = await _floor_with_point(db, building_a.id, "Térreo", "Sala A")
    floor_b, point_b = await _floor_with_point(db, building_b.id, "Térreo", "Sala B")

    worker_a = FieldWorker(full_name="Ana", contractor_company_id=company.id)
    worker_b = FieldWorker(full_name="Bruno", contractor_company_id=company.id)
    qr_a = QrCode(floor_id=floor_a, public_code="qr-a", secret=b"s", version=1)
    qr_b = QrCode(floor_id=floor_b, public_code="qr-b", secret=b"s", version=1)
    db.add_all([worker_a, worker_b, qr_a, qr_b])
    await db.flush()

    route_a = Route(
        field_worker_id=worker_a.id, route_date=_NOW.date(), status=RouteStatus.IN_PROGRESS
    )
    route_b = Route(
        field_worker_id=worker_b.id, route_date=_NOW.date(), status=RouteStatus.IN_PROGRESS
    )
    db.add_all([route_a, route_b])
    await db.flush()

    exec_today_a, _ = await _execution(
        db,
        worker_id=worker_a.id,
        route_id=route_a.id,
        point_id=point_a,
        checked_in_at=_NOW,
        source=ExecutionSource.APP,
        review_status=ExecutionReviewStatus.PENDING_REVIEW,
    )
    await _checkin_scan(
        db, execution_id=exec_today_a, qr_code_id=qr_a.id, geo=GeoValidation.OUT_OF_RADIUS
    )

    exec_old_b, _ = await _execution(
        db,
        worker_id=worker_a.id,
        route_id=route_a.id,
        point_id=point_b,
        checked_in_at=_NOW - timedelta(hours=25),
        source=ExecutionSource.APP,
        review_status=ExecutionReviewStatus.NONE,
    )
    await _checkin_scan(
        db, execution_id=exec_old_b, qr_code_id=qr_b.id, geo=GeoValidation.VALIDATED
    )

    exec_manual_a, _ = await _execution(
        db,
        worker_id=worker_b.id,
        route_id=route_b.id,
        point_id=point_a,
        checked_in_at=_NOW - timedelta(hours=1),
        source=ExecutionSource.MANAGER_MANUAL,
        review_status=ExecutionReviewStatus.NONE,
    )

    db.add(
        EvidenceItem(
            execution_id=exec_today_a,
            kind=EvidenceKind.NOTE,
            text_body="Piso molhado.",
            captured_at=_NOW,
        )
    )
    await db.commit()
    return _World(
        worker_a=worker_a.id,
        worker_b=worker_b.id,
        building_a=building_a.id,
        building_b=building_b.id,
        point_a=point_a,
        point_b=point_b,
        exec_today_a=exec_today_a,
        exec_old_b=exec_old_b,
        exec_manual_a=exec_manual_a,
    )


async def test_no_filter_returns_all_newest_first(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(db_session, ExecutionFilters(), page=1, page_size=50)

    assert total == 3
    assert [r.execution_id for r in rows] == [
        world.exec_today_a,
        world.exec_manual_a,
        world.exec_old_b,
    ]


async def test_row_carries_denormalised_columns(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, _ = await list_executions(db_session, ExecutionFilters(), page=1, page_size=50)
    by_id = {r.execution_id: r for r in rows}

    today = by_id[world.exec_today_a]
    assert today.field_worker_name == "Ana"
    assert today.building_name == "Bloco A"
    assert today.service_point_name == "Sala A"
    assert today.geo_validation == "OUT_OF_RADIUS"
    assert today.evidence_count == 1
    assert today.source == "APP"
    # A MANAGER_MANUAL execution has no check-in scan, so no geo verdict.
    assert by_id[world.exec_manual_a].geo_validation is None
    assert by_id[world.exec_manual_a].evidence_count == 0


async def test_filter_by_field_worker(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(field_worker_id=world.worker_a), page=1, page_size=50
    )

    assert total == 2
    assert {r.execution_id for r in rows} == {world.exec_today_a, world.exec_old_b}


async def test_filter_by_building(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(building_id=world.building_a), page=1, page_size=50
    )

    assert total == 2
    assert {r.execution_id for r in rows} == {world.exec_today_a, world.exec_manual_a}


async def test_filter_by_service_point(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(service_point_id=world.point_b), page=1, page_size=50
    )

    assert total == 1
    assert rows[0].execution_id == world.exec_old_b


async def test_filter_by_review_status(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(review_status="PENDING_REVIEW"), page=1, page_size=50
    )

    assert total == 1
    assert rows[0].execution_id == world.exec_today_a


async def test_filter_by_source(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(source="MANAGER_MANUAL"), page=1, page_size=50
    )

    assert total == 1
    assert rows[0].execution_id == world.exec_manual_a


async def test_filter_by_geo_validation(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    rows, total = await list_executions(
        db_session, ExecutionFilters(geo_validation="OUT_OF_RADIUS"), page=1, page_size=50
    )

    assert total == 1
    assert rows[0].execution_id == world.exec_today_a


async def test_filter_by_date_range(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)
    today = _NOW.date()

    from_today, from_total = await list_executions(
        db_session, ExecutionFilters(date_from=today), page=1, page_size=50
    )
    to_yesterday, to_total = await list_executions(
        db_session,
        ExecutionFilters(date_to=today - timedelta(days=1)),
        page=1,
        page_size=50,
    )

    assert from_total == 2
    assert {r.execution_id for r in from_today} == {world.exec_today_a, world.exec_manual_a}
    assert to_total == 1
    assert to_yesterday[0].execution_id == world.exec_old_b


async def test_filter_by_route_stop_status(db_session: AsyncSession) -> None:
    await _seed_world(db_session)

    _, done_total = await list_executions(
        db_session, ExecutionFilters(route_stop_status="DONE"), page=1, page_size=50
    )
    _, progress_total = await list_executions(
        db_session, ExecutionFilters(route_stop_status="IN_PROGRESS"), page=1, page_size=50
    )

    assert done_total == 3
    assert progress_total == 0


async def test_pagination_caps_the_page_and_keeps_total(db_session: AsyncSession) -> None:
    await _seed_world(db_session)

    page_one, total = await list_executions(db_session, ExecutionFilters(), page=1, page_size=2)
    page_two, _ = await list_executions(db_session, ExecutionFilters(), page=2, page_size=2)

    assert total == 3
    assert len(page_one) == 2
    assert len(page_two) == 1


async def test_pagination_is_stable_with_equal_timestamps(db_session: AsyncSession) -> None:
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco A", campus_area="CCT")
    db_session.add_all([company, building])
    await db_session.flush()
    _, point = await _floor_with_point(db_session, building.id, "Térreo", "Sala A")
    worker = FieldWorker(full_name="Ana", contractor_company_id=company.id)
    db_session.add(worker)
    await db_session.flush()
    route = Route(field_worker_id=worker.id, route_date=_NOW.date(), status=RouteStatus.IN_PROGRESS)
    db_session.add(route)
    await db_session.flush()

    same_instant = _NOW
    ids = set()
    for _ in range(3):
        execution_id, _ = await _execution(
            db_session,
            worker_id=worker.id,
            route_id=route.id,
            point_id=point,
            checked_in_at=same_instant,
            source=ExecutionSource.APP,
            review_status=ExecutionReviewStatus.NONE,
        )
        ids.add(execution_id)
    await db_session.commit()

    page_one, total = await list_executions(db_session, ExecutionFilters(), page=1, page_size=2)
    page_two, _ = await list_executions(db_session, ExecutionFilters(), page=2, page_size=2)

    assert total == 3
    seen = [r.execution_id for r in page_one] + [r.execution_id for r in page_two]
    assert len(seen) == 3
    assert set(seen) == ids


async def test_unknown_enum_filter_raises_value_error(db_session: AsyncSession) -> None:
    await _seed_world(db_session)

    with pytest.raises(ValueError, match="BOGUS"):
        await list_executions(
            db_session, ExecutionFilters(review_status="BOGUS"), page=1, page_size=50
        )


async def test_get_execution_detail_bundles_scans_and_evidence(
    db_session: AsyncSession,
) -> None:
    world = await _seed_world(db_session)

    detail = await get_execution_detail(db_session, world.exec_today_a)

    assert detail is not None
    assert detail.execution.execution_id == world.exec_today_a
    assert [s.kind for s in detail.scans] == ["CHECK_IN"]
    assert detail.scans[0].geo_validation == "OUT_OF_RADIUS"
    assert [e.kind for e in detail.evidence] == ["NOTE"]
    assert detail.manual_completion is None


async def _seed_form_version(db: AsyncSession, *, known_key: uuid.UUID) -> uuid.UUID:
    service_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    db.add(service_type)
    await db.flush()
    form = Form(service_type_id=service_type.id, name="Inspeção de limpeza")
    db.add(form)
    await db.flush()
    version = FormVersion(form_id=form.id, status=FormVersionStatus.PUBLISHED, version_number=1)
    db.add(version)
    await db.flush()
    db.add(
        FormQuestion(
            form_version_id=version.id,
            stable_key=known_key,
            order_index=0,
            prompt="Área limpa?",
            question_type=QuestionType.BOOLEAN,
            required=True,
            options=[],
        )
    )
    await db.flush()
    return version.id


async def test_get_execution_detail_surfaces_form_answers(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)
    known_key = uuid.uuid4()
    version_id = await _seed_form_version(db_session, known_key=known_key)

    execution = await db_session.get(Execution, world.exec_old_b)
    assert execution is not None
    execution.form_version_id = version_id
    db_session.add_all(
        [
            Answer(
                execution_id=world.exec_old_b,
                question_stable_key=str(known_key),
                value_json=True,
            ),
            Answer(
                execution_id=world.exec_old_b,
                question_stable_key=str(uuid.uuid4()),  # not in the version → prompt None
                value_json="observação",
            ),
        ]
    )
    await db_session.commit()

    detail = await get_execution_detail(db_session, world.exec_old_b)

    assert detail is not None
    assert detail.form_version_id == version_id
    assert len(detail.answers) == 2
    known = next(a for a in detail.answers if a.question_stable_key == str(known_key))
    assert known.prompt == "Área limpa?"
    assert known.value is True
    removed = next(a for a in detail.answers if a.question_stable_key != str(known_key))
    assert removed.prompt is None
    assert removed.value == "observação"


async def test_get_execution_detail_without_form_has_no_answers(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    detail = await get_execution_detail(db_session, world.exec_today_a)

    assert detail is not None
    assert detail.form_version_id is None
    assert detail.answers == []


async def test_get_execution_detail_missing_returns_none(db_session: AsyncSession) -> None:
    await _seed_world(db_session)

    assert await get_execution_detail(db_session, uuid.uuid4()) is None


async def test_resolve_review_moves_pending_to_resolved(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    execution = await resolve_review(db_session, execution_id=world.exec_today_a)

    assert execution.review_status is ExecutionReviewStatus.RESOLVED


async def test_resolve_review_rejects_non_pending(db_session: AsyncSession) -> None:
    world = await _seed_world(db_session)

    with pytest.raises(ReviewNotPending):
        await resolve_review(db_session, execution_id=world.exec_old_b)


async def test_resolve_review_missing_execution_raises(db_session: AsyncSession) -> None:
    await _seed_world(db_session)

    with pytest.raises(ExecutionNotFound):
        await resolve_review(db_session, execution_id=uuid.uuid4())
